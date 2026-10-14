#!/usr/bin/env bash
set -u
P="${FPV_PROJECT:-$HOME/detector_drone}"
errors=0
check() { if "$@" >/dev/null 2>&1; then printf '[OK] %s\n' "$*";else printf '[PENDIENTE] %s\n' "$*";errors=$((errors+1));fi; }
echo 'FPV 1.0 baseline: fpvdec 30FPS, TFT SPI 24MHz, Web V4 30FPS, TAK V3.3.2'
check test -x "$P/.venv-tft/bin/python"
check test -x "$P/v19e_fps30_experimental/build-fps30/fpvdec"
check test -f "$P/fpv_tak_config.json"
check test -e /dev/spidev0.1
check systemctl is-active --quiet detector-fpv
check systemctl is-active --quiet fpv-web-tft
check systemctl is-active --quiet fpv-tak-sensor
check test -e /dev/shm/fpv_status.json
check test -e /dev/shm/fpv_tak_status.json
printf 'SPI config: ';cat "$P/fpv_v19e_consolidado/spi_hz.txt" 2>/dev/null || echo 'SIN CONFIGURAR'
command -v vcgencmd >/dev/null && { vcgencmd get_throttled;vcgencmd measure_temp; } || true
journalctl -u detector-fpv -n 2 --no-pager 2>/dev/null || true
printf 'Pendientes=%d (GNSS/certificados pueden no estar activos intencionadamente)\n' "$errors"
