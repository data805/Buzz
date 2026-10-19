#!/usr/bin/env bash
# Default-deny firewall for the Buzz hosts (F5).
#
# Containers: egress filtered in the DOCKER-USER chain (Docker-published ports
# bypass ufw and the INPUT chain, so this is the only place to filter them).
# Host: INPUT allow-list, and optionally an OUTPUT allow-list (Asterisk runs
# with network_mode: host, so its traffic is host traffic).
#
# Usage (as root):
#   docker-user-egress.sh --config app.conf --apply           apply (idempotent)
#   docker-user-egress.sh --config app.conf --dry-run         print the rules only
#   docker-user-egress.sh --remove --dry-run                  print what --remove would do
#   docker-user-egress.sh --config app.conf --trial 120       apply; remove after
#                                                             120 s unless confirmed
#   docker-user-egress.sh --config app.conf --remove          remove everything we added
#   docker-user-egress.sh --open-egress [--minutes N]         temporarily allow all egress
#   docker-user-egress.sh --close-egress                      end it (so does --apply)
#   docker-user-egress.sh --status                            show our chains and counters
#
# Chains we own: BUZZ-DOCKER, BUZZ-CT-OUT (DOCKER-USER), BUZZ-INPUT, BUZZ-OUTPUT,
# and BUZZ-INPUT6, BUZZ-OUTPUT6, BUZZ-DOCKER6 for IPv6. Nothing else is touched
# apart from one jump rule at the top of DOCKER-USER, INPUT and OUTPUT.
set -euo pipefail

SCRIPT_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
OPEN_TAG="buzz-open-egress"
DRY_RUN=0
ACTION=""
CONFIG=""
TRIAL_SECONDS=0
OPEN_MINUTES=0

usage() { sed -n '2,25p' "$SCRIPT_PATH"; }
die() { echo "buzz-firewall: $*" >&2; exit 1; }
log() { echo "buzz-firewall: $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) CONFIG="${2:?--config needs a file}"; shift ;;
        --apply) ACTION=apply ;;
        --dry-run) DRY_RUN=1 ;;
        --trial) ACTION=apply; TRIAL_SECONDS="${2:?--trial needs seconds}"; shift ;;
        --remove) ACTION=remove ;;
        --open-egress) ACTION=open ;;
        --close-egress) ACTION=close ;;
        --minutes) OPEN_MINUTES="${2:?--minutes needs a number}"; shift ;;
        --status) ACTION=status ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1 (try --help)" ;;
    esac
    shift
done
[[ -n "$ACTION" || $DRY_RUN -eq 0 ]] || ACTION=apply
[[ -n "$ACTION" ]] || { usage; exit 2; }
case "$ACTION:$DRY_RUN" in open:1|close:1|status:1) die "--dry-run works with --apply and --remove only" ;; esac

# ---------------------------------------------------------------------------
# Command runner: prints in dry-run, executes otherwise.
# ---------------------------------------------------------------------------
IPT=iptables
IP6T=ip6tables

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "$*"
    else
        "$@"
    fi
}

# Create (or empty) a chain.
fresh_chain() {
    local ipt=$1 chain=$2
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "$ipt -N $chain   # if missing"
        echo "$ipt -F $chain"
        return
    fi
    $ipt -N "$chain" 2>/dev/null || true
    $ipt -F "$chain"
}

# Ensure exactly one jump from PARENT to CHAIN, at position 1.
ensure_jump() {
    local ipt=$1 parent=$2 chain=$3
    if [[ $DRY_RUN -eq 1 ]]; then
        echo "$ipt -I $parent 1 -j $chain   # once"
        return
    fi
    while $ipt -C "$parent" -j "$chain" 2>/dev/null; do
        $ipt -D "$parent" -j "$chain"
    done
    $ipt -I "$parent" 1 -j "$chain"
}

drop_jump() {
    local ipt=$1 parent=$2 chain=$3
    if [[ $DRY_RUN -eq 1 ]]; then echo "$ipt -D $parent -j $chain   # while present"; return; fi
    while $ipt -C "$parent" -j "$chain" 2>/dev/null; do
        $ipt -D "$parent" -j "$chain"
    done
}

drop_chain() {
    local ipt=$1 chain=$2
    if [[ $DRY_RUN -eq 1 ]]; then echo "$ipt -F $chain; $ipt -X $chain"; return; fi
    if $ipt -L "$chain" -n >/dev/null 2>&1; then
        $ipt -F "$chain"
        $ipt -X "$chain"
    fi
}

# ---------------------------------------------------------------------------
# Config parsing helpers
# ---------------------------------------------------------------------------
CIDR4_RE='^([0-9]{1,3}\.){3}[0-9]{1,3}(/[0-9]{1,2})?$'
PORT_RE='^[0-9]{1,5}(-[0-9]{1,5})?$'

check_cidr() { [[ "$1" =~ $CIDR4_RE ]] || die "not an IPv4 address/CIDR: '$1'"; }
check_port() { [[ "$1" =~ $PORT_RE ]] || die "not a port or range: '$1'"; }
check_proto() { [[ "$1" == tcp || "$1" == udp ]] || die "protocol must be tcp or udp: '$1'"; }
ipt_port() { echo "${1/-/:}"; }   # 10000-20000 -> 10000:20000

load_config() {
    [[ -n "$CONFIG" ]] || die "--config is required for --$ACTION"
    [[ -f "$CONFIG" ]] || die "config not found: $CONFIG"
    # Defaults; the config overrides them.
    ROLE=""
    ADMIN_CIDRS=""
    ADMIN_CIDRS6=""
    SSH_PORT=22
    LAN_CIDRS=""
    DNS_SERVERS=""
    NTP_SERVERS=""
    DOCKER_SUBNETS="172.30.0.0/24 172.17.0.0/16"
    PUBLISHED_ALLOW=""
    HOST_INPUT_ALLOW=""
    SIP_CARRIER_IPS=""
    EGRESS_ALLOW=""
    HOST_EGRESS_POLICY=deny
    HOST_EGRESS_ALLOW=""
    ALLOW_LETSENCRYPT=no
    LETSENCRYPT_HOSTS="acme-v02.api.letsencrypt.org"
    INPUT_POLICY=drop
    IPV6_POLICY=drop
    LOG_DROPS=yes
    # shellcheck disable=SC1090
    . "$CONFIG"

    [[ "$ROLE" == app || "$ROLE" == inference ]] || die "ROLE must be app or inference"
    [[ "$HOST_EGRESS_POLICY" == deny || "$HOST_EGRESS_POLICY" == allow ]] || die "HOST_EGRESS_POLICY must be deny or allow"
    [[ "$INPUT_POLICY" == drop || "$INPUT_POLICY" == return ]] || die "INPUT_POLICY must be drop or return"
    [[ "$IPV6_POLICY" == drop || "$IPV6_POLICY" == ignore ]] || die "IPV6_POLICY must be drop or ignore"
    [[ "$SSH_PORT" =~ ^[0-9]+$ ]] || die "SSH_PORT must be a number"
    local c
    for c in $ADMIN_CIDRS $LAN_CIDRS $DNS_SERVERS $NTP_SERVERS $DOCKER_SUBNETS $SIP_CARRIER_IPS; do
        check_cidr "$c"
    done
    if [[ "$INPUT_POLICY" == drop && -z "$ADMIN_CIDRS" ]]; then
        die "ADMIN_CIDRS is empty with INPUT_POLICY=drop: you would lock yourself out of SSH"
    fi
    [[ -n "$DNS_SERVERS" ]] || log "warning: DNS_SERVERS is empty; containers will not resolve external names"
}

# Rate-limited log, then drop/reject.
deny_tail() {
    local ipt=$1 chain=$2 prefix=$3 target=$4
    if [[ "$LOG_DROPS" == yes ]]; then
        run $ipt -A "$chain" -m limit --limit 10/min --limit-burst 20 -j LOG --log-prefix "$prefix " --log-level 4
    fi
    if [[ "$target" == REJECT ]]; then
        # Fast failure for applications instead of hanging on timeouts.
        if [[ "$ipt" == "$IPT" ]]; then
            run $ipt -A "$chain" -p tcp -j REJECT --reject-with tcp-reset
            run $ipt -A "$chain" -j REJECT --reject-with icmp-admin-prohibited
        else
            run $ipt -A "$chain" -p tcp -j REJECT --reject-with tcp-reset
            run $ipt -A "$chain" -j REJECT --reject-with icmp6-adm-prohibited
        fi
    else
        run $ipt -A "$chain" -j DROP
    fi
}

# Spec "cidr" or "cidr:proto:port" -> rule(s) in CHAIN with TARGET.
add_dest_spec() {
    local chain=$1 spec=$2 target=$3 cidr proto port
    IFS=: read -r cidr proto port <<<"$spec"
    check_cidr "$cidr"
    if [[ -z "${proto:-}" ]]; then
        run $IPT -A "$chain" -d "$cidr" -j "$target"
    else
        check_proto "$proto"; check_port "$port"
        run $IPT -A "$chain" -d "$cidr" -p "$proto" --dport "$(ipt_port "$port")" -j "$target"
    fi
}

resolve_v4() {
    local host=$1
    getent ahostsv4 "$host" 2>/dev/null | awk '{print $1}' | sort -u
}

# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
apply_docker_chains() {
    local spec proto port src c

    # DOCKER-USER exists once dockerd has started. Creating it early is
    # harmless: Docker adopts the existing chain.
    if [[ $DRY_RUN -eq 0 ]] && ! $IPT -L DOCKER-USER -n >/dev/null 2>&1; then
        log "DOCKER-USER does not exist yet (is Docker running?); creating it"
        $IPT -N DOCKER-USER
    fi

    fresh_chain $IPT BUZZ-CT-OUT
    fresh_chain $IPT BUZZ-DOCKER

    # ---- BUZZ-DOCKER: everything forwarded to or from containers ----------
    run $IPT -A BUZZ-DOCKER -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    run $IPT -A BUZZ-DOCKER -m conntrack --ctstate INVALID -j DROP
    # New connections started by containers: goto, so RETURN in BUZZ-CT-OUT
    # goes straight back to DOCKER-USER (and on to Docker's own rules).
    run $IPT -A BUZZ-DOCKER -i docker0 -g BUZZ-CT-OUT
    run $IPT -A BUZZ-DOCKER -i br-+ -g BUZZ-CT-OUT
    # New inbound connections to published ports. After Docker's DNAT the
    # destination port is the container's, so match the original port.
    for spec in $PUBLISHED_ALLOW; do
        IFS=: read -r proto port src <<<"$spec"
        check_proto "$proto"; check_port "$port"; check_cidr "$src"
        run $IPT -A BUZZ-DOCKER -p "$proto" -s "$src" -m conntrack --ctstate DNAT \
            --ctorigdstport "$(ipt_port "$port")" -j RETURN
    done
    deny_tail $IPT BUZZ-DOCKER "BUZZ-IN-DROP" DROP

    # ---- BUZZ-CT-OUT: egress from containers ------------------------------
    for c in $DOCKER_SUBNETS; do
        run $IPT -A BUZZ-CT-OUT -d "$c" -j RETURN   # container to container, hairpin to published ports
    done
    for c in $DNS_SERVERS; do
        run $IPT -A BUZZ-CT-OUT -d "$c" -p udp --dport 53 -j RETURN
        run $IPT -A BUZZ-CT-OUT -d "$c" -p tcp --dport 53 -j RETURN
    done
    for c in $LAN_CIDRS; do
        run $IPT -A BUZZ-CT-OUT -d "$c" -j RETURN
    done
    for spec in $EGRESS_ALLOW; do
        add_dest_spec BUZZ-CT-OUT "$spec" RETURN
    done
    deny_tail $IPT BUZZ-CT-OUT "BUZZ-EGRESS-DROP" REJECT

    ensure_jump $IPT DOCKER-USER BUZZ-DOCKER
}

apply_host_input() {
    local spec proto port src c
    fresh_chain $IPT BUZZ-INPUT
    run $IPT -A BUZZ-INPUT -i lo -j ACCEPT
    run $IPT -A BUZZ-INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    run $IPT -A BUZZ-INPUT -m conntrack --ctstate INVALID -j DROP
    run $IPT -A BUZZ-INPUT -p icmp --icmp-type echo-request -m limit --limit 5/s -j ACCEPT
    run $IPT -A BUZZ-INPUT -p icmp --icmp-type destination-unreachable -j ACCEPT
    run $IPT -A BUZZ-INPUT -p icmp --icmp-type time-exceeded -j ACCEPT
    for c in $ADMIN_CIDRS; do
        run $IPT -A BUZZ-INPUT -s "$c" -p tcp --dport "$SSH_PORT" -j ACCEPT
    done
    # SIP and RTP to Asterisk (host network) from the carrier only.
    for c in $SIP_CARRIER_IPS; do
        run $IPT -A BUZZ-INPUT -s "$c" -p udp --dport 5060 -j ACCEPT
        run $IPT -A BUZZ-INPUT -s "$c" -p tcp --dport 5060:5061 -j ACCEPT
        run $IPT -A BUZZ-INPUT -s "$c" -p udp --dport 10000:20000 -j ACCEPT
    done
    for spec in $HOST_INPUT_ALLOW; do
        IFS=: read -r proto port src <<<"$spec"
        check_proto "$proto"; check_port "$port"; check_cidr "$src"
        run $IPT -A BUZZ-INPUT -s "$src" -p "$proto" --dport "$(ipt_port "$port")" -j ACCEPT
    done
    if [[ "$INPUT_POLICY" == drop ]]; then
        deny_tail $IPT BUZZ-INPUT "BUZZ-INPUT-DROP" DROP
    fi
    # INPUT_POLICY=return: fall through to ufw / the existing INPUT rules.
    ensure_jump $IPT INPUT BUZZ-INPUT
}

apply_host_output() {
    local spec c ip host
    if [[ "$HOST_EGRESS_POLICY" == allow ]]; then
        drop_jump $IPT OUTPUT BUZZ-OUTPUT
        drop_chain $IPT BUZZ-OUTPUT
        return
    fi
    fresh_chain $IPT BUZZ-OUTPUT
    run $IPT -A BUZZ-OUTPUT -o lo -j ACCEPT
    run $IPT -A BUZZ-OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    run $IPT -A BUZZ-OUTPUT -p icmp -j ACCEPT
    for c in $DOCKER_SUBNETS $LAN_CIDRS; do
        run $IPT -A BUZZ-OUTPUT -d "$c" -j ACCEPT
    done
    for c in $DNS_SERVERS; do
        run $IPT -A BUZZ-OUTPUT -d "$c" -p udp --dport 53 -j ACCEPT
        run $IPT -A BUZZ-OUTPUT -d "$c" -p tcp --dport 53 -j ACCEPT
    done
    for c in $NTP_SERVERS; do
        run $IPT -A BUZZ-OUTPUT -d "$c" -p udp --dport 123 -j ACCEPT
    done
    # Asterisk signalling and media to the carrier.
    for c in $SIP_CARRIER_IPS; do
        run $IPT -A BUZZ-OUTPUT -d "$c" -j ACCEPT
    done
    if [[ "$ALLOW_LETSENCRYPT" == yes ]]; then
        # certbot runs on the host. ACME addresses change: re-run --apply
        # from certbot's pre-hook so they are re-resolved before renewal.
        for host in $LETSENCRYPT_HOSTS; do
            ip="$(resolve_v4 "$host")"
            [[ -n "$ip" ]] || { log "warning: cannot resolve $host; Let's Encrypt will fail"; continue; }
            for c in $ip; do
                run $IPT -A BUZZ-OUTPUT -d "$c" -p tcp --dport 443 -m comment --comment "letsencrypt $host" -j ACCEPT
            done
        done
    fi
    for spec in $HOST_EGRESS_ALLOW; do
        add_dest_spec BUZZ-OUTPUT "$spec" ACCEPT
    done
    deny_tail $IPT BUZZ-OUTPUT "BUZZ-HOST-EGRESS-DROP" REJECT
    ensure_jump $IPT OUTPUT BUZZ-OUTPUT
}

apply_ipv6() {
    if [[ "$IPV6_POLICY" == ignore ]]; then
        remove_ipv6
        return
    fi
    if [[ $DRY_RUN -eq 0 ]] && ! command -v $IP6T >/dev/null 2>&1; then log "ip6tables not found; skipping IPv6"; return; fi
    local c
    # Host: loopback, established, ICMPv6 (neighbour discovery needs it),
    # SSH from ADMIN_CIDRS6; nothing else in or out.
    fresh_chain $IP6T BUZZ-INPUT6
    run $IP6T -A BUZZ-INPUT6 -i lo -j ACCEPT
    run $IP6T -A BUZZ-INPUT6 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    run $IP6T -A BUZZ-INPUT6 -p ipv6-icmp -j ACCEPT
    for c in $ADMIN_CIDRS6; do
        run $IP6T -A BUZZ-INPUT6 -s "$c" -p tcp --dport "$SSH_PORT" -j ACCEPT
    done
    deny_tail $IP6T BUZZ-INPUT6 "BUZZ-INPUT6-DROP" DROP
    ensure_jump $IP6T INPUT BUZZ-INPUT6

    fresh_chain $IP6T BUZZ-OUTPUT6
    run $IP6T -A BUZZ-OUTPUT6 -o lo -j ACCEPT
    run $IP6T -A BUZZ-OUTPUT6 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    run $IP6T -A BUZZ-OUTPUT6 -p ipv6-icmp -j ACCEPT
    deny_tail $IP6T BUZZ-OUTPUT6 "BUZZ-OUTPUT6-DROP" REJECT
    ensure_jump $IP6T OUTPUT BUZZ-OUTPUT6

    # Containers: no IPv6 forwarding at all.
    if [[ $DRY_RUN -eq 1 ]] || $IP6T -L DOCKER-USER -n >/dev/null 2>&1; then
        fresh_chain $IP6T BUZZ-DOCKER6
        run $IP6T -A BUZZ-DOCKER6 -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
        deny_tail $IP6T BUZZ-DOCKER6 "BUZZ-DOCKER6-DROP" DROP
        ensure_jump $IP6T DOCKER-USER BUZZ-DOCKER6
    fi
}

remove_ipv6() {
    [[ $DRY_RUN -eq 1 ]] || command -v $IP6T >/dev/null 2>&1 || return 0
    drop_jump $IP6T INPUT BUZZ-INPUT6
    drop_jump $IP6T OUTPUT BUZZ-OUTPUT6
    if [[ $DRY_RUN -eq 1 ]] || $IP6T -L DOCKER-USER -n >/dev/null 2>&1; then
        drop_jump $IP6T DOCKER-USER BUZZ-DOCKER6
    fi
    drop_chain $IP6T BUZZ-INPUT6
    drop_chain $IP6T BUZZ-OUTPUT6
    drop_chain $IP6T BUZZ-DOCKER6
}

do_remove() {
    drop_jump $IPT INPUT BUZZ-INPUT
    drop_jump $IPT OUTPUT BUZZ-OUTPUT
    if [[ $DRY_RUN -eq 1 ]] || $IPT -L DOCKER-USER -n >/dev/null 2>&1; then
        drop_jump $IPT DOCKER-USER BUZZ-DOCKER
    fi
    drop_chain $IPT BUZZ-DOCKER
    drop_chain $IPT BUZZ-CT-OUT
    drop_chain $IPT BUZZ-INPUT
    drop_chain $IPT BUZZ-OUTPUT
    remove_ipv6
    log "removed all Buzz firewall chains"
}

do_apply() {
    load_config
    [[ $DRY_RUN -eq 1 ]] && echo "# Buzz firewall rules for ROLE=$ROLE from $CONFIG (dry run, nothing applied)"
    apply_docker_chains
    apply_host_input
    apply_host_output
    apply_ipv6
    [[ $DRY_RUN -eq 1 ]] || log "applied ($ROLE). Any temporary open egress is now closed."
}

# ---------------------------------------------------------------------------
# Temporary open egress (model downloads, image pulls, apt upgrades)
# ---------------------------------------------------------------------------
do_open() {
    $IPT -L BUZZ-CT-OUT -n >/dev/null 2>&1 || die "BUZZ-CT-OUT missing; apply the firewall first"
    do_close quiet
    run $IPT -I BUZZ-CT-OUT 1 -m comment --comment "$OPEN_TAG" -j RETURN
    if $IPT -L BUZZ-OUTPUT -n >/dev/null 2>&1; then
        run $IPT -I BUZZ-OUTPUT 1 -m comment --comment "$OPEN_TAG" -j ACCEPT
    fi
    log "EGRESS IS OPEN (containers and host). Close it with --close-egress."
    if [[ "$OPEN_MINUTES" =~ ^[1-9][0-9]*$ ]]; then
        if command -v systemd-run >/dev/null 2>&1; then
            systemd-run --quiet --on-active="${OPEN_MINUTES}m" --unit="buzz-close-egress-$$" \
                "$SCRIPT_PATH" --close-egress
            log "scheduled automatic close in $OPEN_MINUTES min (systemd unit buzz-close-egress-$$)"
        else
            nohup bash -c "sleep $((OPEN_MINUTES * 60)); '$SCRIPT_PATH' --close-egress" >/dev/null 2>&1 &
            log "scheduled automatic close in $OPEN_MINUTES min (background pid $!)"
        fi
    fi
}

do_close() {
    local chain target n=0
    for chain in BUZZ-CT-OUT:RETURN BUZZ-OUTPUT:ACCEPT; do
        target=${chain#*:}; chain=${chain%%:*}
        while $IPT -C "$chain" -m comment --comment "$OPEN_TAG" -j "$target" 2>/dev/null; do
            $IPT -D "$chain" -m comment --comment "$OPEN_TAG" -j "$target"
            n=$((n + 1))
        done
    done
    [[ "${1:-}" == quiet ]] || log "egress closed ($n rule(s) removed)"
}

do_status() {
    local chain
    for chain in BUZZ-DOCKER BUZZ-CT-OUT BUZZ-INPUT BUZZ-OUTPUT; do
        $IPT -L "$chain" -n -v --line-numbers 2>/dev/null || echo "(no chain $chain)"
        echo
    done
    if command -v $IP6T >/dev/null 2>&1; then
        for chain in BUZZ-DOCKER6 BUZZ-INPUT6 BUZZ-OUTPUT6; do
            $IP6T -L "$chain" -n -v 2>/dev/null || echo "(no chain $chain)"
            echo
        done
    fi
    if $IPT -S BUZZ-CT-OUT 2>/dev/null | grep -q "$OPEN_TAG"; then
        echo "WARNING: egress is currently OPEN"
    fi
}

# ---------------------------------------------------------------------------
if [[ $DRY_RUN -eq 0 && $EUID -ne 0 ]]; then
    die "must run as root (or use --dry-run)"
fi
if [[ $DRY_RUN -eq 0 ]]; then
    command -v $IPT >/dev/null 2>&1 || die "iptables not found"
fi

case "$ACTION" in
    apply)
        do_apply
        if [[ "$TRIAL_SECONDS" =~ ^[1-9][0-9]*$ && $DRY_RUN -eq 0 ]]; then
            log "trial: check SSH and the services from ANOTHER session now."
            if read -r -t "$TRIAL_SECONDS" -p "Type 'keep' within $TRIAL_SECONDS s to keep the rules: " answer \
                && [[ "$answer" == keep ]]; then
                log "kept"
            else
                echo
                log "no confirmation; removing the rules"
                do_remove
                exit 1
            fi
        fi
        ;;
    remove) do_remove ;;
    open) do_open ;;
    close) do_close ;;
    status) do_status ;;
esac
