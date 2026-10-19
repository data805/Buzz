#!/usr/bin/env bash
# Container health: Asterisk fully booted and the modules Buzz depends on running.
# Does not check the carrier (a carrier outage should alert, not restart Asterisk).
set -euo pipefail

rx() { timeout 8 asterisk -rx "$1" 2>/dev/null; }

rx "core waitfullybooted" | grep -q "fully booted" || { echo "not fully booted"; exit 1; }

for mod in res_ari.so chan_websocket.so res_websocket_client.so chan_pjsip.so func_curl.so res_prometheus.so; do
    line="$(rx "module show like ${mod}" | grep -F "${mod}" || true)"
    case "$line" in
        *"Not Running"*|"") echo "${mod} not running"; exit 1 ;;
        *Running*) : ;;
        *) echo "${mod} state unknown"; exit 1 ;;
    esac
done

rx "pjsip show endpoint zm-trunk" | grep -Eq "Endpoint: +zm-trunk" || { echo "zm-trunk endpoint missing"; exit 1; }
rx "dialplan show buzz-outbound" | grep -q "buzz-outbound" || { echo "dialplan not loaded"; exit 1; }
echo ok
