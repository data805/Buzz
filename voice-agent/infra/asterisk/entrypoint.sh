#!/usr/bin/env bash
# Buzz Asterisk entrypoint.
#
#   entrypoint.sh run                       render config, wait for the ARI bind address, exec Asterisk (default)
#   entrypoint.sh render <src> <dest>       render templates only (used by tests; no sound or network checks)
#   entrypoint.sh check                     validate the environment and exit
#
# Rendering uses envsubst with an explicit variable list (config/env.list), so
# dialplan variables such as ${EXTEN} survive untouched.
#
# Keep this file compatible with bash 3.2 so the tests also run on macOS.

set -euo pipefail

TEMPLATE_DIR="${BUZZ_TEMPLATE_DIR:-/opt/buzz/asterisk/templates}"
LOCAL_DIR="${BUZZ_LOCAL_DIR:-/opt/buzz/asterisk/local}"
CONF_DIR="${BUZZ_CONF_DIR:-/etc/asterisk}"
SOUNDS_DIR="${BUZZ_SOUNDS_DIR:-/var/lib/asterisk/sounds/buzz}"
ZONEINFO_DIR="${BUZZ_ZONEINFO_DIR:-/usr/share/zoneinfo}"
BIND_WAIT_SECONDS="${BUZZ_BIND_WAIT_SECONDS:-120}"

# Prompts that must exist before Asterisk may take calls (compliance: no
# disclosure, no calls). Names are without extension; .ulaw or .wav accepted.
REQUIRED_SOUNDS="disclosure-inbound disclosure-outbound"

log() { printf '[entrypoint] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

DAY_RE='(mon|tue|wed|thu|fri|sat|sun)'
TIME_RE='([01][0-9]|2[0-3]):[0-5][0-9]'
MONTH_RE='(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)'

trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

# ---------------------------------------------------------------------------
# Environment: defaults and validation
# ---------------------------------------------------------------------------

ENV_NAMES=""

validate_cidrs() {
    local value="$1" item
    local IFS=','
    for item in $value; do
        item="$(trim "$item")"
        [[ "$item" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}(/([0-9]|[12][0-9]|3[0-2]))?$ ]] || return 1
    done
    return 0
}

validate_hours() {
    local value="$1" rule days times
    local IFS=';'
    for rule in $value; do
        rule="$(trim "$rule")"
        [ -z "$rule" ] && continue
        days="${rule%% *}"
        times="$(trim "${rule#* }")"
        [[ "$days" =~ ^${DAY_RE}(-${DAY_RE})?$ ]] || return 1
        [[ "$times" =~ ^${TIME_RE}-${TIME_RE}$ ]] || return 1
    done
    return 0
}

validate_days() {
    local value="$1" item
    local IFS=','
    for item in $value; do
        item="$(trim "$item")"
        [ -z "$item" ] && continue
        [[ "$item" =~ ^([1-9]|[12][0-9]|3[01])\ ${MONTH_RE}$ ]] || return 1
    done
    return 0
}

validate_one() {
    local name="$1" rule="$2" value="$3"
    case "$rule" in
        secret)
            [[ "$value" =~ ^[A-Za-z0-9._~+=-]{16,}$ ]] || die "$name must be 16+ characters from [A-Za-z0-9._~+=-] (try: openssl rand -hex 32)"
            case "$value" in *change*me*|*CHANGE*ME*) die "$name still holds the example placeholder" ;; esac ;;
        required)
            [ -n "$value" ] || die "$name is required" ;;
        optional) : ;;
        int)
            [[ "$value" =~ ^[0-9]+$ ]] || die "$name must be a whole number (got '$value')" ;;
        ipv4)
            [[ "$value" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "$name must be an IPv4 address (got '$value')" ;;
        cidrs)
            [ -n "$value" ] || die "$name is required"
            validate_cidrs "$value" || die "$name must be comma-separated IPv4 addresses or CIDRs (got '$value')" ;;
        stasis)
            [[ "$value" =~ ^dograh_[A-Za-z0-9_]+$ ]] || die "$name must be the Stasis App Name Dograh generated (dograh_...), got '$value'" ;;
        wsuri)
            [[ "$value" =~ ^wss?://[^?#[:space:]]+$ ]] || die "$name must be a ws:// or wss:// URL without a query string (Dograh appends its own)" ;;
        httpurl)
            [[ "$value" =~ ^https?://[^[:space:]]+$ ]] || die "$name must be an http(s) URL (got '$value')" ;;
        choice:*)
            local allowed="/${rule#choice:}/"
            case "$allowed" in *"/$value/"*) : ;; *) die "$name must be one of ${rule#choice:} (got '$value')" ;; esac ;;
        hours)
            validate_hours "$value" || die "$name must look like 'mon-fri 08:00-17:59; sat 09:00-12:59' (got '$value')" ;;
        tz)
            [ -f "$ZONEINFO_DIR/$value" ] || die "$name '$value' is not a known time zone (no $ZONEINFO_DIR/$value)" ;;
        days)
            validate_days "$value" || die "$name must look like '1 jan, 25 dec' (got '$value')" ;;
        *) die "env.list: unknown rule '$rule' for $name" ;;
    esac
}

load_env() {
    local list="$TEMPLATE_DIR/env.list" line name default rule current
    [ -f "$list" ] || die "missing $list"
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        name="${line%%|*}"; line="${line#*|}"
        default="${line%%|*}"; rule="${line#*|}"
        case "$name" in BUZZ_*) die "env.list: $name must not use the BUZZ_ prefix" ;; esac
        current="${!name-}"
        if [ -z "$current" ] && [ -n "$default" ]; then
            current="$default"
        fi
        # Derived default: From domain falls back to the trunk host.
        if [ "$name" = "ZM_TRUNK_FROM_DOMAIN" ] && [ -z "$current" ]; then
            current="${ZM_TRUNK_HOST-}"
        fi
        export "$name=$current"
        validate_one "$name" "$rule" "$current"
        ENV_NAMES="$ENV_NAMES $name"
    done < "$list"

    # Cross-field rules.
    if [ -n "${ZM_TRUNK_USERNAME}" ] && [ -z "${ZM_TRUNK_PASSWORD}" ]; then
        die "ZM_TRUNK_PASSWORD is required when ZM_TRUNK_USERNAME is set"
    fi
    if [ "${ZM_TRUNK_REGISTER}" = "yes" ] && [ -z "${ZM_TRUNK_USERNAME}" ]; then
        die "ZM_TRUNK_REGISTER=yes needs ZM_TRUNK_USERNAME and ZM_TRUNK_PASSWORD"
    fi
    if [ -n "${ZM_TRUNK_PASSWORD}" ]; then
        validate_one ZM_TRUNK_PASSWORD secret "${ZM_TRUNK_PASSWORD}"
    fi
    if [ -n "${MESSAGE_WEBHOOK_URL}" ]; then
        validate_one MESSAGE_WEBHOOK_URL httpurl "${MESSAGE_WEBHOOK_URL}"
        validate_one MESSAGE_WEBHOOK_SECRET secret "${MESSAGE_WEBHOOK_SECRET}"
    fi
    if [ -n "${OUTBOUND_CLI}" ]; then
        [[ "${OUTBOUND_CLI}" =~ ^\+?[0-9]{3,15}$ ]] || die "OUTBOUND_CLI must be digits with an optional leading + (got '${OUTBOUND_CLI}')"
    fi
    [ "${ZM_TRUNK_MAX_CHANNELS}" -ge 1 ] || die "ZM_TRUNK_MAX_CHANNELS must be at least 1"
    # The ARI originate timeout Dograh uses for outbound calls is Asterisk's
    # default of 30 s; ringing plus the disclosure must fit inside it.
    [ "${OUTBOUND_RING_TIMEOUT}" -le 20 ] || die "OUTBOUND_RING_TIMEOUT must be 20 s or less (ARI originate times out at 30 s, and the disclosure plays inside that window)"
}

envsubst_list() {
    local name out=""
    for name in $ENV_NAMES; do out="$out \${$name}"; done
    printf '%s' "$out"
}

# ---------------------------------------------------------------------------
# Generated include files
# ---------------------------------------------------------------------------

# extensions_hours.conf: [buzz-sub-hours] returns "open" or "closed".
# GotoIfTime end minutes are inclusive, so 08:00-17:59 means until 18:00.
write_hours() {
    local out="$1" rule days times item day month
    {
        echo "; Generated by entrypoint.sh from CALL_HOURS, CALL_TIMEZONE and CALL_CLOSED_DAYS. Do not edit."
        echo "; CALL_HOURS=${CALL_HOURS}"
        echo "; CALL_CLOSED_DAYS=${CALL_CLOSED_DAYS}"
        echo "[buzz-sub-hours]"
        echo "exten => s,1,NoOp(Checking permitted calling hours in ${CALL_TIMEZONE})"
        local IFS=','
        for item in ${CALL_CLOSED_DAYS}; do
            item="$(trim "$item")"
            [ -z "$item" ] && continue
            day="${item%% *}"; month="${item##* }"
            echo " same => n,GotoIfTime(*,*,${day},${month},${CALL_TIMEZONE}?closed)"
        done
        IFS=';'
        for rule in ${CALL_HOURS}; do
            rule="$(trim "$rule")"
            [ -z "$rule" ] && continue
            days="${rule%% *}"
            times="$(trim "${rule#* }")"
            echo " same => n,GotoIfTime(${times},${days},*,*,${CALL_TIMEZONE}?open)"
        done
        echo " same => n(closed),Return(closed)"
        echo " same => n(open),Return(open)"
    } > "$out"
}

# pjsip_trunk_auth.conf: only when the carrier wants digest auth or registration.
write_trunk_auth() {
    local out="$1"
    {
        echo "; Generated by entrypoint.sh from ZM_TRUNK_USERNAME / ZM_TRUNK_REGISTER. Do not edit."
        if [ -n "${ZM_TRUNK_USERNAME}" ]; then
            echo "[zm-trunk-auth]"
            echo "type=auth"
            echo "auth_type=digest"
            echo "username=${ZM_TRUNK_USERNAME}"
            echo "password=${ZM_TRUNK_PASSWORD}"
            echo ""
            echo "[zm-trunk](+)"
            echo "outbound_auth=zm-trunk-auth"
            echo "from_user=${ZM_TRUNK_USERNAME}"
            if [ "${ZM_TRUNK_REGISTER}" = "yes" ]; then
                echo ""
                echo "[zm-trunk-reg]"
                echo "type=registration"
                echo "transport=transport-udp"
                echo "outbound_auth=zm-trunk-auth"
                echo "server_uri=sip:${ZM_TRUNK_HOST}:${ZM_TRUNK_PORT}"
                echo "client_uri=sip:${ZM_TRUNK_USERNAME}@${ZM_TRUNK_HOST}"
                echo "retry_interval=60"
                echo "forbidden_retry_interval=600"
                echo "expiration=3600"
            fi
        else
            echo "; IP-authenticated trunk: no auth section."
        fi
    } > "$out"
}

# Operator-maintained files (not templated; may hold staff SIP passwords).
copy_local() {
    local dest="$1" f
    for f in pjsip_staff.conf extensions_dids.conf extensions_transfer_targets.conf; do
        if [ -f "$LOCAL_DIR/$f" ]; then
            cp "$LOCAL_DIR/$f" "$dest/$f"
        else
            log "WARNING: $LOCAL_DIR/$f not found; using an empty one"
            case "$f" in
                extensions_dids.conf) printf '; no DIDs configured: every inbound call is rejected\n[buzz-inbound-dids]\n' > "$dest/$f" ;;
                extensions_transfer_targets.conf) printf '; no special transfer targets\n[buzz-transfer-targets]\n' > "$dest/$f" ;;
                *) printf '; no staff extensions configured\n' > "$dest/$f" ;;
            esac
        fi
    done
}

render() {
    local src="$1" dest="$2" tpl vars
    mkdir -p "$dest"
    vars="$(envsubst_list)"
    for tpl in "$src"/*.conf; do
        envsubst "$vars" < "$tpl" > "$dest/$(basename "$tpl")"
    done
    write_hours "$dest/extensions_hours.conf"
    write_trunk_auth "$dest/pjsip_trunk_auth.conf"
    copy_local "$dest"
    chmod 0640 "$dest"/*.conf
    log "rendered $(ls "$dest"/*.conf | wc -l | tr -d ' ') config files into $dest"
}

check_sounds() {
    local s
    for s in $REQUIRED_SOUNDS; do
        if [ ! -f "$SOUNDS_DIR/$s.ulaw" ] && [ ! -f "$SOUNDS_DIR/$s.wav" ]; then
            die "required prompt $SOUNDS_DIR/$s.(ulaw|wav) is missing; Asterisk will not take calls without the disclosure (see sounds/README.md)"
        fi
    done
}

# The ARI HTTP server binds to the buzz-app bridge gateway (172.30.0.1),
# which exists only after the app compose stack has created the network.
wait_for_bind_address() {
    local waited=0
    until ip -o -4 addr show | grep -q "inet ${ASTERISK_HTTP_BIND}/"; do
        if [ "$waited" -ge "$BIND_WAIT_SECONDS" ]; then
            die "${ASTERISK_HTTP_BIND} is not assigned on this host after ${BIND_WAIT_SECONDS}s; start the app stack (buzz-app network) first"
        fi
        [ "$waited" -eq 0 ] && log "waiting for ${ASTERISK_HTTP_BIND} (buzz-app network gateway) to appear"
        sleep 2
        waited=$((waited + 2))
    done
}

main() {
    local cmd="${1:-run}"
    case "$cmd" in
        render)
            [ $# -eq 3 ] || die "usage: entrypoint.sh render <template-dir> <dest-dir>"
            TEMPLATE_DIR="$2"
            load_env
            render "$2" "$3"
            ;;
        check)
            load_env
            check_sounds
            log "environment OK"
            ;;
        run)
            load_env
            check_sounds
            render "$TEMPLATE_DIR" "$CONF_DIR"
            mkdir -p /var/spool/asterisk/buzz-messages
            wait_for_bind_address
            log "starting Asterisk"
            exec /usr/sbin/asterisk -f -vvv
            ;;
        *)
            exec "$@"
            ;;
    esac
}

main "$@"
