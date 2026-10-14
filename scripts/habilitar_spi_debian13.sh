#!/usr/bin/env bash
# Explicit, opt-in boot configuration on Debian 13 Raspberry Pi 5.
set -Eeuo pipefail
[[ "${EUID}" -eq 0 ]] || { echo 'Uso: sudo bash scripts/habilitar_spi_debian13.sh' >&2; exit 2; }
config=/boot/firmware/config.txt
[[ -f "$config" && ! -L "$config" ]] || { echo "No existe config.txt regular: $config" >&2; exit 2; }
if [[ -e /dev/spidev0.1 ]]; then
    echo '/dev/spidev0.1 ya disponible; no hay cambios.'
    exit 0
fi
if grep -Eq '^[[:space:]]*dtparam=spi=on([[:space:]]*(#.*)?)?$' "$config"; then
    echo 'Ya hay dtparam=spi=on; verifica overlays y reinicia antes de continuar.'
    exit 1
fi
backup="${config}.bak-turnkey-$(date -u +%Y%m%dT%H%M%SZ)"
cp -a -- "$config" "$backup"
printf '\n# Turnkey FPV XPT2046 (SPI0 CE1 / GPIO7)\n[all]\ndtparam=spi=on\n' >> "$config"
echo "Respaldo: $backup"
echo 'SPI habilitado en config.txt. Reinicia con sudo reboot; luego verifica /dev/spidev0.1.'
