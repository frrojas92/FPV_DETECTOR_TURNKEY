#!/usr/bin/env bash
set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if (($#==0)); then
  echo 'FPV RELEASE v1.0.0-reference'
  echo '  bash instalar.sh --check     # comprobar fuente final, paquetes y hardware'
  echo '  bash instalar.sh --prepare   # instalar codigo y compilar C++ final'
  echo '  bash instalar.sh --activate  # activar servicios (interrumpe detector)'
  echo '  bash scripts/capturar_referencia.sh  # SOLO en la RPi que funciona'
  exit 0
fi
exec /usr/bin/python3 "$HERE/scripts/instalar.py" "$@"
