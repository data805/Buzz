#!/usr/bin/env python3
"""Fail on any network traffic to or from an address outside the allow-list.

Input: text from ``tcpdump -nn [-tttt] [-i any] -r capture.pcap`` (or a live
``tcpdump -nn``). No scapy and no root needed. Understands IPv4 and IPv6 lines
in both the classic form and the ``-i any`` form, which adds the interface and
direction::

    2026-10-19 10:00:00.000001 IP 10.0.0.5.443 > 41.72.1.2.51234: Flags [P.], ...
    2026-10-19 10:00:00.000002 eth0 Out IP 10.0.0.5.40000 > 104.16.1.1.443: Flags [S], ...
    10:00:00.000003 IP6 fe80::1.546 > ff02::1:2.547: dhcp6 solicit
    10:00:00.000004 IP 10.0.0.5 > 8.8.8.8: ICMP echo request, id 1, seq 1, length 64

Rules:
- An endpoint is *allowed* if it falls in ``allowed_networks`` (LAN, Docker,
  loopback, link-local, multicast), ``carrier_networks`` (SIP trunk) or
  ``extra_allowed``.
- A packet is fine if both endpoints are allowed.
- Inbound service traffic is fine: an unknown remote talking to a *local*
  address on one of ``allowed_inbound_ports`` (e.g. 443 for staff browsers,
  3478/5349 for TURN). Only list ports that are really public.
- Everything else is a violation, reported once per (remote, port, protocol)
  with a packet count and first-seen timestamp.

Exit status: 0 clean, 1 violations, 2 bad input or config.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

import yaml

DEFAULT_CONFIG = Path(__file__).with_name("egress_allowlist.yaml")

LINE_RE = re.compile(
    r"^(?P<ts>(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}(?:\.\d+)?)?\s*"
    r"(?:(?P<iface>\S+) (?P<dir>In|Out|M|P|B|H)\s+)?"
    r"(?P<fam>IP6?) (?P<src>\S+) > (?P<dst>\S+?):(?:\s+|$)(?P<rest>.*)$"
)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class Packet:
    ts: str
    family: str
    src: Address
    sport: int | None
    dst: Address
    dport: int | None
    proto: str
    line_no: int


@dataclass
class Violation:
    remote: str
    remote_port: int | None
    local: str
    local_port: int | None
    proto: str
    direction: str  # "outbound" (remote is destination) or "inbound"
    first_seen: str
    first_line: int
    count: int = 0

    def format(self) -> str:
        rp = f":{self.remote_port}" if self.remote_port is not None else ""
        lp = f":{self.local_port}" if self.local_port is not None else ""
        arrow = "->" if self.direction == "outbound" else "<-"
        return (f"{self.direction:8} {self.proto:5} {self.local}{lp} {arrow} {self.remote}{rp}  "
                f"packets={self.count} first={self.first_seen or '?'} (line {self.first_line})")


def split_endpoint(token: str, family: str) -> tuple[Address, int | None]:
    """'10.0.0.5.443' -> (10.0.0.5, 443); '10.0.0.5' -> (.., None); 'fe80::1.546' -> (fe80::1, 546)."""
    token = token.rstrip(",:")
    if family == "IP":
        parts = token.split(".")
        if len(parts) == 5:
            return ipaddress.ip_address(".".join(parts[:4])), _port(parts[4])
        return ipaddress.ip_address(token), None
    # IPv6 addresses contain no dots except the IPv4-mapped tail; tcpdump appends ".port".
    if "." in token:
        head, _, tail = token.rpartition(".")
        try:
            return ipaddress.ip_address(head), _port(tail)
        except ValueError:
            return ipaddress.ip_address(token), None
    return ipaddress.ip_address(token), None


def _port(text: str) -> int | None:
    return int(text) if text.isdigit() else None  # tcpdump without -nn prints service names


def guess_proto(rest: str) -> str:
    upper = rest.upper()
    if rest.startswith("Flags ["):
        return "tcp"
    if upper.startswith("ICMP"):
        return "icmp"
    if upper.startswith(("UDP", "SIP", "NTP", "BOOTP", "DHCP")) or "LENGTH" in upper or re.match(r"^\d+[+*%-]* ", rest):
        return "udp"  # UDP decoders print "length N"; DNS lines start with the query id
    return "ip"


def parse(lines: Iterable[str]) -> Iterator[Packet | tuple[int, str]]:
    """Yield Packet for IP lines; (line_no, line) for lines that look like IP but failed to parse."""
    for n, raw in enumerate(lines, 1):
        line = raw.rstrip("\n")
        m = LINE_RE.match(line)
        if not m:
            continue  # ARP, LLDP, STP, continuation lines, tcpdump banners
        fam = m.group("fam")
        try:
            src, sport = split_endpoint(m.group("src"), fam)
            dst, dport = split_endpoint(m.group("dst"), fam)
        except ValueError:
            yield (n, line)
            continue
        yield Packet(m.group("ts") or "", fam, src, sport, dst, dport, guess_proto(m.group("rest")), n)


@dataclass
class Policy:
    allowed: list[Network]
    local: set[Address]
    inbound_ports: set[int] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path, environ: dict[str, str] | None = None) -> "Policy":
        env = os.environ if environ is None else environ
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        def expand(values) -> list[str]:
            out = []
            for v in values or []:
                s = re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), ""), str(v)).strip()
                out += [x.strip() for x in s.split(",") if x.strip()]
            return out

        nets = [ipaddress.ip_network(x, strict=False) for key in ("allowed_networks", "carrier_networks", "extra_allowed")
                for x in expand(raw.get(key))]
        local = {ipaddress.ip_address(x) for x in expand(raw.get("local_addresses"))}
        ports = {int(p) for p in raw.get("allowed_inbound_ports") or []}
        return cls(nets, local, ports)

    def is_allowed(self, addr: Address) -> bool:
        return addr in self.local or any(addr.version == net.version and addr in net for net in self.allowed)


def analyse(lines: Iterable[str], policy: Policy) -> tuple[list[Violation], int, list[tuple[int, str]]]:
    violations: "OrderedDict[tuple, Violation]" = OrderedDict()
    unparsed: list[tuple[int, str]] = []
    packets = 0
    for item in parse(lines):
        if isinstance(item, tuple):
            unparsed.append(item)
            continue
        packets += 1
        p = item
        src_ok, dst_ok = policy.is_allowed(p.src), policy.is_allowed(p.dst)
        if src_ok and dst_ok:
            continue
        if src_ok and not dst_ok:
            # Local side is the source. A reply from a public service port is inbound traffic.
            if p.sport in policy.inbound_ports:
                continue
            key = (str(p.dst), p.dport, p.proto, "outbound")
            v = violations.setdefault(key, Violation(str(p.dst), p.dport, str(p.src), p.sport, p.proto,
                                                     "outbound", p.ts, p.line_no))
        elif dst_ok and not src_ok:
            if p.dport in policy.inbound_ports:
                continue
            key = (str(p.src), p.dport, p.proto, "inbound")  # one entry per client and local port
            v = violations.setdefault(key, Violation(str(p.src), p.sport, str(p.dst), p.dport, p.proto,
                                                     "inbound", p.ts, p.line_no))
        else:
            key = (str(p.dst), p.dport, p.proto, "transit")
            v = violations.setdefault(key, Violation(str(p.dst), p.dport, str(p.src), p.sport, p.proto,
                                                     "transit", p.ts, p.line_no))
        v.count += 1
    return list(violations.values()), packets, unparsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check tcpdump -nn text output against the egress allow-list.")
    parser.add_argument("captures", nargs="+", type=Path, help="tcpdump -nn text files ('-' for stdin)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--min-packets", type=int, default=1,
                        help="fail if a capture holds fewer IP packets than this (catches empty captures)")
    args = parser.parse_args(argv)
    try:
        policy = Policy.load(args.config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"error: config: {exc}")
        return 2
    status = 0
    for path in args.captures:
        stream = sys.stdin if str(path) == "-" else path.open(encoding="utf-8", errors="replace")
        with stream:
            violations, packets, unparsed = analyse(stream, policy)
        print(f"== {path}: {packets} IP packets, {len(violations)} violation(s), {len(unparsed)} unparsed IP line(s)")
        for v in violations:
            print("  " + v.format())
        for n, line in unparsed[:10]:
            print(f"  unparsed line {n}: {line[:160]}")
        if packets < args.min_packets:
            print(f"  FAIL: fewer than {args.min_packets} packets; was the capture running during the test call?")
            status = max(status, 2)
        if violations:
            status = max(status, 1)
    print("RESULT:", {0: "PASS", 1: "FAIL (egress outside allow-list)", 2: "ERROR"}[status])
    return status


if __name__ == "__main__":
    sys.exit(main())
