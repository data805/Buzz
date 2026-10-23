#!/bin/sh
# Render turnserver.conf from the template and start coturn.
# Replaces Dograh's dograh-init container for coturn. POSIX sh: the coturn
# image has no bash. Runs as the image's user; writes only to /tmp.
set -eu

TEMPLATE=/etc/buzz-coturn/turnserver.conf.template
OUT=/tmp/turnserver.conf

fail() { echo "coturn init: $*" >&2; exit 1; }

[ -f "$TEMPLATE" ] || fail "template $TEMPLATE not found"
[ -n "${TURN_SECRET:-}" ] || fail "TURN_SECRET is empty"
[ -n "${TURN_REALM:-}" ] || fail "TURN_REALM is empty"
[ -n "${TURN_EXTERNAL_IP:-}" ] || fail "TURN_EXTERNAL_IP is empty"

# Values go into sed replacements: refuse anything but safe characters.
case "$TURN_SECRET" in *[!A-Za-z0-9]*) fail "TURN_SECRET must be alphanumeric (gen-secrets.sh makes hex)";; esac
case "$TURN_REALM" in *[!A-Za-z0-9.-]*) fail "TURN_REALM must be a hostname";; esac
case "$TURN_EXTERNAL_IP" in *[!0-9.]*) fail "TURN_EXTERNAL_IP must be an IPv4 address";; esac

umask 077
sed -e "s/__TURN_SECRET__/${TURN_SECRET}/g" \
    -e "s/__TURN_REALM__/${TURN_REALM}/g" \
    -e "s/__TURN_EXTERNAL_IP__/${TURN_EXTERNAL_IP}/g" \
    "$TEMPLATE" > "$OUT"

if grep -q '__TURN_' "$OUT"; then
    fail "unrendered placeholder left in $OUT"
fi

echo "coturn init: rendered $OUT (realm ${TURN_REALM}, external-ip ${TURN_EXTERNAL_IP})"
exec turnserver -c "$OUT"
