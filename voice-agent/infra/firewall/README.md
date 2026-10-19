# Buzz host firewall

Default-deny egress for containers, and allow-listed inbound traffic, on both servers (PLAN F5). This is also what makes the "no egress" packet capture in Phase 2 pass.

| File | Purpose |
|---|---|
| `docker-user-egress.sh` | Applies, prints, removes and inspects the rules. Idempotent. |
| `app.conf.example` | Allow-lists for the app host. |
| `inference.conf.example` | Allow-lists for the inference host. |
| `buzz-firewall.service` | systemd unit that re-applies the rules at boot and when Docker restarts. |

## Why ufw is not enough

Docker publishes a port (`ports: "443:443"`) by adding a DNAT rule in the `nat` table. The packet is rewritten to the container address **before** filtering, and then goes through the `FORWARD` chain, not `INPUT`. ufw only filters `INPUT` (and its own forward rules, which Docker's chains come before). So `ufw deny 8000` does **not** close a published port 8000, and ufw has no say over traffic that containers start.

Docker sends every forwarded packet through the `DOCKER-USER` chain first and promises never to change it. That is where these rules go:

- **From containers** (`docker0`, `br-*`), new connections may go only to Docker networks, the DNS resolvers, `LAN_CIDRS` and `EGRESS_ALLOW`. Everything else is logged (`BUZZ-EGRESS-DROP`) and rejected, so applications fail fast instead of hanging.
- **To published ports**, new connections are accepted only where `PUBLISHED_ALLOW` lists the original port and source. On the inference host this is what limits vLLM and Speaches to the app host.

Host services that are not in containers (SSH, Asterisk with `network_mode: host`, node-exporter, Asterisk ARI on `172.30.0.1:8088`) are filtered in `BUZZ-INPUT`. Host egress, which includes Asterisk's SIP and RTP, is filtered in `BUZZ-OUTPUT` when `HOST_EGRESS_POLICY=deny`.

IPv6: with `IPV6_POLICY=drop`, the host allows only loopback, ICMPv6 and SSH from `ADMIN_CIDRS6`, and containers get no IPv6 forwarding.

## Install

On each host, as root:

```
install -d -m 700 /etc/buzz
install -m 600 infra/firewall/app.conf.example /etc/buzz/firewall.conf   # inference.conf.example on the GPU host
$EDITOR /etc/buzz/firewall.conf

install -m 755 infra/firewall/docker-user-egress.sh /usr/local/sbin/buzz-firewall
buzz-firewall --config /etc/buzz/firewall.conf --dry-run     # read every rule
buzz-firewall --config /etc/buzz/firewall.conf --trial 120   # apply; auto-removed unless you type "keep"
```

During `--trial`, open a **second** SSH session and check that you can still log in and that the services answer. If you do not type `keep` within the time, the rules are removed again.

Then make it permanent:

```
install -m 644 infra/firewall/buzz-firewall.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now buzz-firewall
```

Other commands: `buzz-firewall --status` (chains and packet counters), `buzz-firewall --remove` (take everything out; `--remove --dry-run` shows what it would do).

Notes:

- `ADMIN_CIDRS` must not be empty while `INPUT_POLICY=drop`; the script refuses to lock you out.
- If the host gets its address by DHCP, add the DHCP server: `HOST_INPUT_ALLOW="udp:68:<dhcp-server>/32"` and `HOST_EGRESS_ALLOW="<dhcp-server>/32:udp:67"` (verify on the real host). Static addressing is simpler.
- Docker's embedded DNS forwards container lookups to the host's upstream resolvers, so `DNS_SERVERS` must list the resolvers in `/run/systemd/resolve/resolv.conf` (or `/etc/resolv.conf`).
- If Docker is switched to its nftables firewall backend, `DOCKER-USER` does not exist; this script assumes the default iptables backend (iptables-nft is fine).
- Let's Encrypt (`ALLOW_LETSENCRYPT=yes`): the ACME addresses are resolved when the rules are applied and change over time. Re-apply from certbot's pre-hook: `certbot renew --pre-hook "/usr/local/sbin/buzz-firewall --config /etc/buzz/firewall.conf --apply"`.

## Opening egress temporarily

For model downloads (Hugging Face), image pulls, `apt upgrade` or the UI build:

```
buzz-firewall --open-egress --minutes 30   # closes itself after 30 minutes
# ... docker compose pull / huggingface-cli download / apt upgrade ...
buzz-firewall --close-egress               # or close it now
```

`--open-egress` adds one tagged rule at the top of the container and host egress chains. `--close-egress` removes it, and so does any `--apply`. `--status` warns while egress is open. Note the window in the change log for the host, and never leave it open across a call session: during the window, nothing stops Dograh's telemetry if it were switched back on.

## Verifying (F5 and the Phase 2 acceptance check)

1. **Rules in place.** `iptables -S DOCKER-USER` starts with `-j BUZZ-DOCKER`; `buzz-firewall --status` shows the chains and counters.
2. **Published ports really closed.** From a machine that is not on any allow-list:
   - app host: `nmap -Pn -p 5432,6379,8000,8090,9000,9001,3000,9090 <app-ip>`: all filtered or closed.
   - inference host: `nmap -Pn -p 8000,8001,9400 <inference-ip>`: filtered. From the app host, `curl -s http://<inference-ip>:8000/health` answers.
3. **Container egress blocked.**
   ```
   docker run --rm --network buzz-app curlimages/curl:8.10.1 -m 5 -sS https://example.com   # must fail
   docker compose -f infra/app/docker-compose.yml exec api python -c "import urllib.request; urllib.request.urlopen('https://us.i.posthog.com', timeout=5)"   # must fail
   journalctl -k | grep BUZZ-EGRESS-DROP   # shows the attempts
   ```
   (Pull the curl image before applying the rules, or during an open-egress window.)
4. **Packet capture: nothing leaves except allowed traffic.** Run the test session from LAN clients only (sign-in, workflow edit, Test Audio call, inbound and outbound phone calls, recording playback). On the app host:
   ```
   tcpdump -ni any -w /root/egress-$(date +%F).pcap \
     'not (net 10.0.0.0/24 or net 172.30.0.0/24 or net 172.17.0.0/16 or net 127.0.0.0/8 or host <carrier-ip>)'
   ```
   Replace the LAN and carrier addresses with the real ones. The capture must contain no packets at all. Any packet is a finding: identify it with `tcpdump -nr <file>` and fix the source, not the filter. If public clients are reaching the host at the same time, capture only connections the host starts instead: add `and ((tcp[tcpflags] & (tcp-syn|tcp-ack)) == tcp-syn or (udp and not src port 3478 and not src portrange 49152-49200))` and check that the only hits are rejected attempts also seen in `journalctl -k | grep BUZZ-`. Do the same on the inference host during the benchmark. Keep the capture with the phase evidence (`*.pcap` is git-ignored).
