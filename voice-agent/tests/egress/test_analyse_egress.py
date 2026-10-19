"""Unit tests for analyse_egress.py using synthetic tcpdump -nn text."""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import analyse_egress as ae  # noqa: E402

ENV = {
    "APP_LAN_IP": "10.0.0.10",
    "INFERENCE_LAN_IP": "10.0.0.20",
    "APP_PUBLIC_IP": "41.60.1.10",
    "CARRIER_NETWORKS": "196.46.1.0/28, 196.46.2.5/32",
    "DNS_RESOLVERS": "",
    "NTP_SERVERS": "",
}


@pytest.fixture
def policy() -> ae.Policy:
    return ae.Policy.load(HERE / "egress_allowlist.yaml", environ=ENV)


def run(policy, text: str):
    return ae.analyse(text.strip().splitlines(), policy)


CLEAN = """
2026-10-19 10:00:00.000001 eth0 Out IP 10.0.0.10.40312 > 10.0.0.20.8000: Flags [S], seq 1, win 64240, length 0
2026-10-19 10:00:00.000002 eth0 In  IP 10.0.0.20.8000 > 10.0.0.10.40312: Flags [S.], seq 2, ack 2, length 0
2026-10-19 10:00:00.000003 IP 172.30.0.5.51000 > 172.30.0.1.8088: Flags [P.], seq 1:100, ack 1, length 99
2026-10-19 10:00:00.000004 IP 41.60.1.10.5060 > 196.46.1.3.5060: SIP: INVITE sip:+260953017526@carrier SIP/2.0
2026-10-19 10:00:00.000005 IP 196.46.2.5.18000 > 41.60.1.10.12000: UDP, length 172
2026-10-19 10:00:00.000006 IP 102.145.3.4.55123 > 41.60.1.10.443: Flags [S], seq 9, win 64240, length 0
2026-10-19 10:00:00.000007 IP 41.60.1.10.443 > 102.145.3.4.55123: Flags [S.], seq 10, ack 10, length 0
10:00:00.000008 IP6 fe80::1.546 > ff02::1:2.547: dhcp6 solicit
10:00:00.000009 ARP, Request who-has 10.0.0.1 tell 10.0.0.10, length 28
10:00:00.000010 IP 127.0.0.1.8090 > 127.0.0.1.41000: Flags [P.], length 20
10:00:00.000011 IP 0.0.0.0.68 > 255.255.255.255.67: BOOTP/DHCP, Request, length 300
"""


def test_clean_capture(policy):
    violations, packets, unparsed = run(policy, CLEAN)
    assert violations == [] and unparsed == []
    assert packets == 10  # ARP ignored


@pytest.mark.parametrize("line,remote,port,direction,proto", [
    # PostHog (F2) from inside a container, NAT'd out of the app host
    ("2026-10-19 10:00:01.0 eth0 Out IP 41.60.1.10.39000 > 34.120.1.1.443: Flags [S], seq 1, length 0",
     "34.120.1.1", 443, "outbound", "tcp"),
    # Dograh MPS (F4)
    ("10:00:01.0 IP 10.0.0.10.40000 > 104.18.20.30.443: Flags [P.], seq 1:50, length 49", "104.18.20.30", 443, "outbound", "tcp"),
    # Hugging Face download from the inference box
    ("10:00:01.0 IP 10.0.0.20.51111 > 3.163.1.1.443: Flags [S], seq 1, length 0", "3.163.1.1", 443, "outbound", "tcp"),
    # public DNS resolver not on the list
    ("10:00:01.0 IP 10.0.0.10.53000 > 8.8.8.8.53: 1234+ A? us.i.posthog.com. (34)", "8.8.8.8", 53, "outbound", "udp"),
    # ICMP has no ports
    ("10:00:01.0 IP 10.0.0.10 > 1.1.1.1: ICMP echo request, id 1, seq 1, length 64", "1.1.1.1", None, "outbound", "icmp"),
    # SIP from a non-carrier address (toll-fraud scanner) to 5060
    ("10:00:01.0 IP 185.10.10.10.5070 > 41.60.1.10.5060: SIP: OPTIONS sip:100@41.60.1.10 SIP/2.0", "185.10.10.10", 5070, "inbound", "udp"),
    # IPv6 egress
    ("10:00:01.0 IP6 fd12:3456::10.40000 > 2606:4700::1111.443: Flags [S], seq 1, length 0", "2606:4700::1111", 443, "outbound", "tcp"),
])
def test_violations(policy, line, remote, port, direction, proto):
    violations, _, _ = run(policy, line)
    assert len(violations) == 1
    v = violations[0]
    assert (v.remote, v.remote_port, v.direction, v.proto) == (remote, port, direction, proto)
    assert v.count == 1


def test_duplicates_are_collapsed(policy):
    text = "\n".join(
        f"10:00:0{i}.0 IP 10.0.0.10.4000{i} > 34.120.1.1.443: Flags [P.], length 10" for i in range(5)
    )
    violations, packets, _ = run(policy, text)
    assert packets == 5 and len(violations) == 1 and violations[0].count == 5
    assert violations[0].first_line == 1


def test_inbound_from_one_client_collapses_across_ephemeral_ports(policy):
    text = "\n".join(f"10:00:00.0 IP 185.1.1.1.{50000 + i} > 41.60.1.10.22: Flags [S], length 0" for i in range(3))
    violations, _, _ = run(policy, text)
    assert len(violations) == 1 and violations[0].count == 3 and violations[0].local_port == 22


def test_reply_from_public_port_is_allowed_but_other_ports_are_not(policy):
    ok = "10:00:00.0 IP 41.60.1.10.3478 > 102.1.1.1.60000: UDP, length 100"
    bad = "10:00:00.0 IP 41.60.1.10.8000 > 102.1.1.1.60000: Flags [S.], length 0"
    assert run(policy, ok)[0] == []
    assert len(run(policy, bad)[0]) == 1


def test_carrier_list_from_env_is_split(policy):
    assert policy.is_allowed(ipaddress.ip_address("196.46.1.15"))
    assert not policy.is_allowed(ipaddress.ip_address("196.46.1.16"))
    assert policy.is_allowed(ipaddress.ip_address("196.46.2.5"))


def test_unparseable_ip_line_is_reported(policy):
    _, _, unparsed = run(policy, "10:00:00.0 IP not-an-ip.80 > 10.0.0.1.80: Flags [S], length 0")
    assert len(unparsed) == 1


@pytest.mark.parametrize("token,family,addr,port", [
    ("10.0.0.5.443", "IP", "10.0.0.5", 443),
    ("10.0.0.5", "IP", "10.0.0.5", None),
    ("fe80::1.546", "IP6", "fe80::1", 546),
    ("ff02::1:2", "IP6", "ff02::1:2", None),
    ("10.0.0.5.https", "IP", "10.0.0.5", None),  # without -nn; port unknown
])
def test_split_endpoint(token, family, addr, port):
    a, p = ae.split_endpoint(token, family)
    assert str(a) == addr and p == port


def test_cli(tmp_path, monkeypatch, capsys):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    clean = tmp_path / "app.txt"
    clean.write_text(CLEAN, encoding="utf-8")
    assert ae.main([str(clean)]) == 0
    assert "RESULT: PASS" in capsys.readouterr().out
    dirty = tmp_path / "inference.txt"
    dirty.write_text(CLEAN + "10:00:01.0 IP 10.0.0.20.5000 > 3.163.1.1.443: Flags [S], length 0\n", encoding="utf-8")
    assert ae.main([str(clean), str(dirty)]) == 1
    out = capsys.readouterr().out
    assert "3.163.1.1:443" in out and "RESULT: FAIL" in out
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    assert ae.main([str(empty), "--min-packets", "1"]) == 2
