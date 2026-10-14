# Instalación de una Raspberry Pi idéntica

**Objetivo:** Raspberry Pi 5 de 8 GB, Debian 13 trixie 64 bits, cuenta `cetel`, hostname libre, red local administrada por SSH. Verificar versión de firmware/kernel en la captura de referencia; los drivers GPIO pueden depender de ella.

## Cableado conocido

- ILI9341: SPI0; `board.D5` como TFT CS, `board.D25` como DC, `board.D24` como RST, alimentación lógica 3,3 V; cableado físico debe validarse en la placa real.
- Táctil XPT2046: SPI0 CE1 `/dev/spidev0.1` (GPIO7 / pin físico 26), SCLK GPIO11, DIN GPIO10, DOUT GPIO9, PENIRQ GPIO4, 3,3 V.
- HackRF One: USB propio, identificador habitual `1d50:6089`. **No usar antena GPIO**.
- Audio: dongle KT USB Audio en USB; salida analógica 3,5 mm; ajustar mixer ALSA y confirmar que `amixer` expone controles. `fpv-usb-volume.service` puede no aplicar al adaptador sin controles hardware.
- GNSS NEO-6M: **opcional/no presume cableado realizado**; configurar servicio `gpsd` con puerto real y comprobar fix antes de activar `position.mode=auto`. La ausencia de fix no se reemplaza por una posición ficticia.

## En el prototipo original que ya funciona

1. Descarga la release, descomprímela y ejecuta `bash scripts/capturar_referencia.sh` desde la carpeta del ZIP.
2. Comprueba `vendor/fpvdec/src/main.cpp`, `vendor/fpvdec/LICENSE`, `reference/requirements.lock.txt` y `reference/reference-manifest.json`.
3. Inspecciona las fuentes y licencias y prepara tu repositorio Git privado/público según tus políticas de publicación.
4. No subas tus certificados, configuración privada, calibraciones particulares ni el archivo SoftCert ZIP.

## En la Pi nueva

```bash
# Preparación física previa, habilitar SPI y red SSH, usuario cetel
sudo raspi-config # si disponible; habilitar SPI y reiniciar
ls -l /dev/spidev0.0 /dev/spidev0.1
lsusb
aplay -l
# Instalación de paquetes y entorno
bash scripts/dependencias_debian13.sh
python3 scripts/instalar.py --check
python3 scripts/instalar.py --prepare
# Conectar transmisor de prueba solo tras calibración inicial
cd ~/detector_drone && ./iniciar_detector_drone_v19e_consolidado.sh
# Detener con q y después activar
cd ~/FPV_DETECTOR_TURNKEY_V1_0 && python3 scripts/instalar.py --activate
```

El instalador usa fuente C++ final capturada y compila con CMake. Si existe `~/detector_drone/v19_lab/.local-sdl2` usará la configuración privada de SDL2; si no, intentará usar `libsdl2-dev` del sistema. **No está demostrado todavía que ambas variantes produzcan el mismo rendimiento**. Un fallo de compilación obliga a revisar dependencias, no a sustituir silenciosamente fpvdec.

Si `.venv-tft` ya existe, **no** se actualiza. Si no existe, se crea con `reference/requirements.lock.txt` capturado. Cambios de versión en Debian/pip afectan la reproducibilidad; registrar cualquier diferencia.

## Servicios finales (sin overrides históricos)

- `fpv-boot-splash.service` splash V3 30 s, terminal headless.
- `detector-fpv.service` tmux + RAMTEST consolidado + SPI 24 MHz.
- `fpv-web-tft.service` puerto 8765, MJPEG 30 FPS sin autenticación.
- `fpv-tak-sensor.service` CoT/TLS o UDP local hasta cargar SoftCert, TTL volátil 12 s.
- `fpv-usb-volume.service` ALSA, su efectividad depende del control de volumen del dongle.

El instalador archiva las unidades anteriores y suprime **solo overrides reconocidos** que pertenecen a etapas históricas (V2, V3, FPS30). Si hay un override desconocido, aborta antes de interrumpir servicios.

## Activación inicial TAK

```bash
fpv-tak
# 1 importar ZIP (responder Sí a --legacy si el PKCS#12 lo requiere)
# 2 callsign; 5 GNSS o lat/lon fijas; 6 TTL; 7 TLS
# 8 STOP TAK y caducidad ordenada; 9 START TAK
```

Para TLS normal sin TUI: `fpv-tak-softcert /ruta/Auto_Cert.zip --legacy` solo cuando corresponda. El importador conserva credenciales en `~/detector_drone/tak_certs/` (chmod 0700/0600). Su conexión valida la CA y el hostname del certificado.

Si la sesión SSH usa IPv4 actual, no apliques en caliente cambios de IP: el TUI prepara el perfil `nmcli` diferidamente y documenta su aplicación posterior.

## Apagado

`sudo shutdown -h now`, esperar fin de actividad de la microSD antes de quitar alimentación.

## Recuperación de servicios

`--activate` almacena las unidades anteriores y overrides conocidos en `~/detector_drone/backups_turnkey/services-<fecha>/`. Para restaurarlos manualmente:

```bash
bash scripts/restaurar_servicios.sh ~/detector_drone/backups_turnkey/services-AAAAMMDD-HHMMSS
```

Este script detiene servicios y restaura las unidades; tras inspeccionarlas, el operador debe iniciarlas. El contenido del proyecto respaldado por `--prepare` se guarda por separado bajo `~/detector_drone/backups_turnkey/<fecha>/`; su recuperación es manual y debe reconciliarse con cambios posteriores de configuración TAK. Nunca revertir certificados antiguos automáticamente.
