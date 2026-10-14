#!/usr/bin/env bash
# Restore the service units backed up by scripts/instalar.py --activate.
set -Eeuo pipefail
[[ $# -eq 1 ]] || { echo 'Uso: bash scripts/restaurar_servicios.sh ~/detector_drone/backups_turnkey/services-AAAAMMDD-HHMMSS';exit 2; }
BACKUP="$(realpath -e -- "$1")"
[[ "$BACKUP" == "$HOME"/detector_drone/backups_turnkey/services-* ]] || { echo 'Solo se admiten backups del instalador FPV.' >&2;exit 2; }
[[ -d "$BACKUP" ]] || exit 2
printf 'Restaurará servicios previos y reiniciará el detector. Escribe RESTAURAR: '
read -r ANSWER
[[ "$ANSWER" == RESTAURAR ]] || { echo Cancelado;exit 1; }
sudo -v
for service in fpv-tak-sensor fpv-web-tft detector-fpv; do sudo systemctl stop "$service" 2>/dev/null || true; done
for service in fpv-boot-splash detector-fpv fpv-web-tft fpv-tak-sensor fpv-usb-volume; do
  UNIT="/etc/systemd/system/$service.service"
  if [[ -f "$BACKUP/$service.service.bak" ]]; then
    sudo install -m 0644 "$BACKUP/$service.service.bak" "$UNIT"
  else
    sudo rm -f -- "$UNIT"
  fi
  for item in "$BACKUP/$service."*.conf.bak; do
    [[ -e "$item" ]] || continue
    f="${item##*/}"
    conf="${f#"$service."}"
    conf="${conf%.bak}"
    dir="/etc/systemd/system/$service.service.d"
    sudo install -d -m 0755 "$dir"
    sudo install -m 0644 "$item" "$dir/$conf"
  done
done
sudo systemctl daemon-reload
printf 'Unidades y overrides previos restaurados. Comprueba el contenido y arranca lo que corresponda:\n'
printf '  systemctl cat detector-fpv fpv-boot-splash fpv-tak-sensor\n'
printf '  sudo systemctl start detector-fpv fpv-web-tft fpv-tak-sensor\n'
printf 'IMPORTANTE: este script no revierte el código ni los secretos respaldados por --prepare.\n'
