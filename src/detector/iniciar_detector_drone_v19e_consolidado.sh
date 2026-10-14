#!/usr/bin/env bash
# RPi5 + HackRF + ILI9341/XPT2046 + fpvdec experimental color backend.
set -Eeuo pipefail
DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
DETECTOR="$DIR/fpv58_detector19e.py"
TFT="$DIR/display_tft_v19e_fps30.py"
FPVDEC="$DIR/v19e_fps30_experimental/build-fps30/fpvdec"
SPI_FILE="$DIR/fpv_v19e_consolidado/spi_hz.txt"
TFT_SPI="${FPV_TFT_BAUDRATE:-$(cat "$SPI_FILE")}"
case "$TFT_SPI" in
  12000000|18000000|24000000|32000000) ;;
  *) echo "SPI invalido: $TFT_SPI" >&2; exit 1 ;;
esac
TFT_PYTHON="$DIR/.venv-tft/bin/python"
LOG="/dev/shm/display_tft_v19e_consolidado.log"

for file in "$DETECTOR" "$TFT" "$TFT_PYTHON" "$FPVDEC"; do
    if [[ ! -e "$file" ]]; then
        printf 'ERROR: falta archivo %s\n' "$file" >&2
        exit 1
    fi
done
if [[ ! -x "$TFT_PYTHON" ]]; then
    echo "ERROR: interprete TFT no ejecutable: $TFT_PYTHON" >&2
    exit 1
fi
if [[ ! -t 0 || ! -t 1 ]]; then
    echo 'ERROR: ejecuta desde una terminal SSH interactiva con TTY.' >&2
    exit 1
fi
if [[ ! -e /dev/spidev0.1 ]]; then
    echo 'ERROR: falta /dev/spidev0.1, requerido para XPT2046 en CE1/GPIO7.' >&2
    exit 1
fi
command -v flock >/dev/null || { echo 'ERROR: falta flock (util-linux).' >&2; exit 1; }
if [[ -z "${TERM:-}" || "${TERM:-}" == dumb || "${TERM:-}" == unknown ]]; then
    export TERM=xterm-256color
fi
mkdir -p "$DIR/logs"
exec 9>"$DIR/.fpv_v6.lock"
if ! flock -n 9; then
    echo 'ERROR: otro lanzador de FPV ya esta en ejecucion.' >&2
    exit 1
fi
TFT_PID=''
cleanup() {
    trap - EXIT INT TERM HUP
    if [[ -n "$TFT_PID" ]] && kill -0 "$TFT_PID" 2>/dev/null; then
        kill -TERM "$TFT_PID" 2>/dev/null || true
        for attempt in {1..15}; do
            kill -0 "$TFT_PID" 2>/dev/null || break
            sleep 0.1
        done
        if kill -0 "$TFT_PID" 2>/dev/null; then
            kill -KILL "$TFT_PID" 2>/dev/null || true
        fi
        wait "$TFT_PID" 2>/dev/null || true
    fi
    echo 'Lanzador V19-E detenido.'
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

echo "SPI CE1 detectado: /dev/spidev0.1"
echo "Log TFT: $LOG"
echo 'Inicializando TFT V19-E BIG VIDEO (CE1/GPIO7)...'
FPV_TFT_BAUDRATE="$TFT_SPI" "$TFT_PYTHON" -u "$TFT" --touch-debug --video-refresh 0.025 --state-file /dev/shm/fpv_status.json </dev/null >>"$LOG" 2>&1 &
TFT_PID=$!
sleep 1.5
if ! kill -0 "$TFT_PID" 2>/dev/null; then
    echo 'ERROR: el proceso TFT finalizo durante el arranque:' >&2
    tail -n 35 "$LOG" >&2 || true
    exit 1
fi

echo "TFT PID=$TFT_PID. Iniciando HackRF y TUI. Salir con q."
echo 'Mantener el transmisor OFF durante la calibracion RF inicial.'

# Si el operador no especifica backend, usar fpvdec por defecto.
extra=()
have_backend=0
for arg in "$@"; do
    if [[ "$arg" == --video-backend ]] || [[ "$arg" == --video-backend=* ]]; then
        have_backend=1
        break
    fi
done
if (( ! have_backend )); then
    extra+=(--video-backend fpvdec)
fi

extra+=(--fpvdec-exe "$FPVDEC" --video-sample-rate 10000000)
echo "FPV CONSOLIDADO: fpvdec FPS30; TFT SPI=$TFT_SPI Hz; video-refresh=25ms"
set +e
python3 "$DETECTOR" --state-file /dev/shm/fpv_status.json --logdir /dev/shm/fpv_v19e_logs "${extra[@]}" "$@"
RESULT=$?
set -e
exit "$RESULT"
