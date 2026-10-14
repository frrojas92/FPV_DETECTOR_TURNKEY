#!/usr/bin/env bash
# Target Raspberry Pi OS/Debian Trixie 13 64-bit; network/apt repository required.
set -Eeuo pipefail
[[ "$(uname -m)" == aarch64 ]] || echo 'AVISO: plataforma distinta a aarch64; el hardware objetivo es Raspberry Pi 5.' >&2
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-venv python3-pip python3-dev python3-numpy \
  python3-pil python3-spidev python3-serial \
  python3-requests python3-pygame python3-gps \
  python3-gpiozero build-essential cmake pkg-config git \
  libhackrf-dev hackrf libsdl2-dev libusb-1.0-0-dev libfftw3-dev \
  tmux alsa-utils fonts-dejavu-core i2c-tools
# For GNSS optionally: sudo apt install gpsd gpsd-clients
# For IPv4 configuration from TAK TUI optionally: sudo apt install network-manager
# Allow use of SPI + GPIO after membership and re-login, depends on system udev.
