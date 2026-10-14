#!/usr/bin/env bash
set -Eeuo pipefail
DIR=/home/cetel/detector_drone
SESSION=detector
SOCKET=fpv_v19e
TMUX=/usr/bin/tmux
LAUNCHER=$DIR/iniciar_detector_drone_v19e_consolidado.sh

[[ -f "$LAUNCHER" ]] || { echo "Falta $LAUNCHER" >&2; exit 1; }
[[ -x "$DIR/.venv-tft/bin/python" ]] || { echo "Falta Python TFT" >&2; exit 1; }
[[ -c /dev/spidev0.1 ]] || { echo "Falta /dev/spidev0.1 (touch)" >&2; exit 1; }
[[ ! -z ${TERM:-} ]] || export TERM=xterm-256color
cd "$DIR"

if "$TMUX" -L "$SOCKET" has-session -t "$SESSION" 2>/dev/null; then
    echo "La sesion tmux ya existe: $SESSION. Evitando doble HackRF" >&2
    exit 1
fi

cleanup() {
    trap - EXIT TERM INT HUP
    "$TMUX" -L "$SOCKET" kill-session -t "$SESSION" >/dev/null 2>&1 || true
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP

"$TMUX" -L "$SOCKET" new-session -d -s "$SESSION" -x 120 -y 40 -c "$DIR" \
    "/bin/bash ./iniciar_detector_drone_v19e_consolidado.sh"

while "$TMUX" -L "$SOCKET" has-session -t "$SESSION" 2>/dev/null; do
    sleep 2
done

echo "La sesion TUI/Detector termino: se reiniciara el servicio." >&2
exit 1
