#!/usr/bin/env bash
# Generate STAGING placeholder prompts with Speaches TTS (Kokoro), converted to
# 8 kHz mono mu-law (.ulaw) for Asterisk. Not for production: the disclosure
# must be a recorded, approved file (sounds/README.md).
#
#   SPEACHES_URL=http://<inference-host>:8001 SPEACHES_API_KEY=... ./generate_placeholders.sh
#
# Needs curl and ffmpeg (or sox). Writes into ./buzz/ next to this script.
set -euo pipefail

SPEACHES_URL="${SPEACHES_URL:?set SPEACHES_URL, e.g. http://10.0.0.20:8001}"
SPEACHES_API_KEY="${SPEACHES_API_KEY:?set SPEACHES_API_KEY}"
TTS_MODEL="${TTS_MODEL:-speaches-ai/Kokoro-82M-v1.0-ONNX}"
TTS_VOICE="${TTS_VOICE:-bf_emma}"
OUT_DIR="${OUT_DIR:-$(cd "$(dirname "$0")" && pwd)/buzz}"
FORCE="${FORCE:-0}"

mkdir -p "$OUT_DIR"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

convert() {
    # $1 input (any format ffmpeg/sox reads), $2 output .ulaw
    if command -v ffmpeg >/dev/null 2>&1; then
        ffmpeg -loglevel error -y -i "$1" -ar 8000 -ac 1 -f mulaw "$2"
    elif command -v sox >/dev/null 2>&1; then
        sox "$1" -r 8000 -c 1 -t ul "$2"
    else
        echo "need ffmpeg or sox" >&2; exit 1
    fi
}

say() {
    # $1 prompt name, $2 text
    local name="$1" text="$2" out="$OUT_DIR/$1.ulaw"
    if [ -f "$out" ] && [ "$FORCE" != "1" ]; then
        echo "skip  $name (exists; FORCE=1 to overwrite)"; return
    fi
    curl -fsS -X POST "$SPEACHES_URL/v1/audio/speech" \
        -H "Authorization: Bearer $SPEACHES_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$(json_escape "$TTS_MODEL")\",\"voice\":\"$(json_escape "$TTS_VOICE")\",\"input\":\"$(json_escape "$text")\",\"response_format\":\"wav\"}" \
        -o "$tmp/$name.wav"
    convert "$tmp/$name.wav" "$out"
    echo "wrote $out"
}

# Disclosures (PLAN.md section 7). Wording is a placeholder until approved; keep each under 8 seconds.
say disclosure-inbound  "Hello, you've reached BongoHive. You're speaking with an AI assistant, and this call may be recorded."
say disclosure-outbound "Hello, this is BongoHive's AI assistant calling. This call may be recorded."
say service-unavailable "Sorry, we can't take your call right now. Please try again later."

# Transfer prompts (staff only hear the first four)
say transfer-intro          "BongoHive AI desk. I have a caller for you."
say transfer-press-1        "Press 1 to take the call. Press 2 and they can leave a message."
say transfer-declined-staff "Okay. They'll be asked to leave a message."
say transfer-topic-programmes   "They're asking about programmes."
say transfer-topic-events       "They're asking about events."
say transfer-topic-membership   "They're asking about membership and coworking."
say transfer-topic-partnerships "They're asking about partnerships."
say transfer-topic-general      "It's a general enquiry."

# Caller-facing take-a-message prompts
say transfer-leave-message  "Sorry, the team can't take your call right now. Please leave your name, number and message after the tone."
say transfer-message-thanks "Thank you. Someone from BongoHive will get back to you."
