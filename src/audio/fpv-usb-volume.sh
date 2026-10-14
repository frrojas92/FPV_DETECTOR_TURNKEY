#!/usr/bin/env bash
# Optional startup mixer restore. Not every USB audio dongle exposes hardware controls.
set -Eeuo pipefail
CARD=''
for _ in $(seq 1 15); do
  CARD="$(cat /proc/asound/cards 2>/dev/null | awk 'BEGIN {IGNORECASE=1} /^[[:space:]]*[0-9]+[[:space:]]*\[/ { if(tolower($0) ~ /kt|usb/) {print $1;exit} }' | tr -d ' ')"
  [[ "$CARD" =~ ^[0-9]+$ ]] && break
  sleep 1
done
[[ "$CARD" =~ ^[0-9]+$ ]] || { echo 'USB Audio sin tarjeta ALSA detectada'; exit 1; }
FOUND=0
for CONTROL in Speaker PCM Master Headphone; do
  if amixer -c "$CARD" sget "$CONTROL" >/dev/null 2>&1; then
    amixer -c "$CARD" sset "$CONTROL" 100% >/dev/null || true
    amixer -c "$CARD" sset "$CONTROL" unmute >/dev/null || true
    FOUND=1
  fi
done
if (( ! FOUND )); then
  echo 'Adaptador USB sin controles ALSA; requiere volumen por software o mixer externo.'
  exit 1
fi
alsactl store "$CARD" || true
