#!/usr/bin/env bash
# Packet-capture acceptance test (PLAN F5 / R4): capture on both hosts while a
# test call runs, then check every packet against egress_allowlist.yaml.
#
# Usage:
#   tests/egress/capture.sh -m 5 -H app=ops@10.0.0.10 -H inference=ops@10.0.0.20 [-i any] [-o DIR]
#
# Needs: ssh access with passwordless sudo for tcpdump and timeout on each host,
# and tcpdump installed there. Locally: python3 with PyYAML
# (tests/egress/requirements.txt). The variables used by egress_allowlist.yaml
# (APP_LAN_IP, INFERENCE_LAN_IP, APP_PUBLIC_IP, CARRIER_NETWORKS, DNS_RESOLVERS,
# NTP_SERVERS) must be exported before the analysis step.
#
# The SSH session that controls the capture is excluded from the filter, so the
# tester's own address does not show up as a violation. Nothing else is filtered.
set -euo pipefail

minutes=5
iface=any
outdir="captures/egress-$(date +%Y%m%d-%H%M%S)"
hosts=()

usage() { sed -n '2,15p' "$0"; exit 2; }
while getopts "m:H:i:o:h" opt; do
  case "$opt" in
    m) minutes="$OPTARG" ;;
    H) hosts+=("$OPTARG") ;;
    i) iface="$OPTARG" ;;
    o) outdir="$OPTARG" ;;
    *) usage ;;
  esac
done
[[ ${#hosts[@]} -gt 0 ]] || usage
[[ "$minutes" =~ ^[0-9]+$ && "$minutes" -ge 1 ]] || { echo "minutes must be a positive integer" >&2; exit 2; }

here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$outdir"
seconds=$((minutes * 60))
pids=()

for spec in "${hosts[@]}"; do
  name="${spec%%=*}"; target="${spec#*=}"
  remote="/tmp/buzz-egress-${name}.pcap"
  # SSH_CLIENT on the remote side holds the controller's address; exclude only that SSH flow.
  ssh "$target" "ctl=\$(echo \$SSH_CLIENT | awk '{print \$1}'); \
    sudo rm -f $remote; \
    sudo timeout ${seconds} tcpdump -nn -i ${iface} -s 128 -w $remote \"not (tcp port 22 and host \$ctl)\" \
      2>/tmp/buzz-egress-${name}.log || [ \$? -eq 124 ]" &
  pids+=("$!")
  echo "[$name] capturing on $target ($iface) for ${minutes} min"
done

echo
echo ">>> Place the test call(s) now (inbound to the front desk DID and one outbound via the API)."
echo ">>> Capture ends in ${minutes} minute(s)."
for pid in "${pids[@]}"; do wait "$pid"; done

status=0
files=()
for spec in "${hosts[@]}"; do
  name="${spec%%=*}"; target="${spec#*=}"
  remote="/tmp/buzz-egress-${name}.pcap"
  ssh "$target" "sudo chmod a+r $remote"
  scp -q "$target:$remote" "$outdir/${name}.pcap"
  # Convert locally if tcpdump exists here; otherwise convert on the host.
  if command -v tcpdump >/dev/null 2>&1; then
    tcpdump -nn -tttt -r "$outdir/${name}.pcap" > "$outdir/${name}.txt" 2>/dev/null
  else
    ssh "$target" "tcpdump -nn -tttt -r $remote" > "$outdir/${name}.txt"
  fi
  ssh "$target" "sudo rm -f $remote"
  files+=("$outdir/${name}.txt")
done

echo
python3 "$here/analyse_egress.py" --config "$here/egress_allowlist.yaml" --min-packets 10 "${files[@]}" \
  | tee "$outdir/report.txt" || status=$?
echo "pcaps and report in $outdir (git-ignored *.pcap; keep report.txt as evidence)"
exit "$status"
