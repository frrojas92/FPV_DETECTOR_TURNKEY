# FPV DETECTOR — Instalación Turnkey en una Raspberry Pi 5 nueva

**Versión:** `v1.0.0-reference` · **Plataforma:** Raspberry Pi 5 de 8 GB, Debian GNU/Linux 13 Trixie de 64 bits (`aarch64`).

Esta guía es exclusivamente para instalar el sistema completo en una **RPi5 nueva con sistema operativo limpio**, usuario `cetel` y sin una instalación previa en `/home/cetel/detector_drone`. El repositorio incluye las fuentes finales de `fpvdec` y el lock de Python: la instalación se realiza íntegramente en la Pi nueva, sin capturar archivos de otra unidad.

Sistema **pasivo** de detección de vídeo FPV analógico 5,8 GHz con HackRF One, Raspberry Pi 5 de 8 GB, pantalla táctil ILI9341/XPT2046, audio USB, visualización web y sensor CoT/ATAK con certificados mTLS. Este repositorio **no** implementa Remote ID ni el escaneo multibanda 1,2/3,3 GHz (propuestas futuras).

## Rendimiento medido en el prototipo original

| Subsistema | Configuración y resultado constatado por el operador |
|---|---|
| Demodulador `fpvdec` headless NTSC | 10 MSPS; publicación **30 FPS**, `dropped_bytes=0` en prueba |
| TFT ILI9341 240×320 | SPI **24 MHz**, **24–25 FPS** durante vídeo |
| Web V4 | MJPEG hasta **30 FPS** en navegador, sin autenticación HTTP |
| Detector/TUI | V19-E RAMTEST; `SCAN ↔ VIDEO` manual; único HackRF |
| Arranque | Splash V3 (30 s tras inicializar Linux), servicios systemd + tmux |
| TAK | SoftCert ZIP → TLS/mTLS; GNSS gpsd opcional / posición fija; CoT sensor + círculo rojo + UAV OTAN **provisional** |
| Alertas TAK | Volátiles: renovaciones cada 3 s y TTL 12 s; cierre ordenado expira CoT si existe conexión; **GeoChat desactivado** |
| Gestión | `fpv` = TUI del detector; `fpv-tak` = TUI de configuración TAK; `fpv-tak-softcert` importa ZIP (admite `--legacy`) |

No se garantiza detectar/identificar drones ni su posición por RF. El símbolo OTAN de UAV se presenta por política del usuario **como hipótesis** anclada al sensor, no como posición ni afiliación verificada de aeronave.

## Instalación en la RPi5 nueva

### 1. Preparar el equipo

Instala Debian 13 Trixie de 64 bits, crea el usuario `cetel` con acceso a `sudo` y habilita SSH y acceso a Internet para descargar el repositorio y las dependencias. Inicia sesión como `cetel`; ejecuta el instalador sin `sudo`.

Con la Pi apagada, conecta la TFT ILI9341 de 2,4 pulgadas (240×320), el táctil XPT2046, un HackRF One y el adaptador KT USB Audio. Usa el [cableado conocido](docs/INSTALACION.md#cableado-conocido). GNSS es opcional. Mantén apagado el transmisor de prueba durante la calibración inicial.

### 2. Descargar el Turnkey e instalar dependencias

```bash
sudo apt-get update
sudo apt-get install -y git
cd /home/cetel
git clone https://github.com/frrojas92/FPV_DETECTOR_TURNKEY.git
cd /home/cetel/FPV_DETECTOR_TURNKEY
bash scripts/dependencias_debian13.sh
```

El código se descarga en `/home/cetel/FPV_DETECTOR_TURNKEY`; el instalador creará la instalación del detector en `/home/cetel/detector_drone`. Este último directorio no debe existir antes de preparar la instalación.

### 3. Habilitar SPI y comprobar el hardware

```bash
ls -l /dev/spidev0.1
id -nG
lsusb
aplay -l
```

Comprueba que aparecen HackRF y el audio USB, que existe `/dev/spidev0.1` y que `cetel` tiene permisos para SPI/GPIO según las reglas del sistema (incluido el grupo `spi` cuando corresponda). Tras cambiar grupos, cierra y vuelve a abrir la sesión SSH.

Si `/dev/spidev0.1` no existe:

```bash
cd /home/cetel/FPV_DETECTOR_TURNKEY
sudo bash scripts/habilitar_spi_debian13.sh
sudo reboot
```

El script respalda `/boot/firmware/config.txt` y habilita SPI. Tras reconectar por SSH, comprueba de nuevo `/dev/spidev0.1` antes de continuar.

### 4. Preparar y calibrar la pantalla

Desde un terminal SSH interactivo y con la TFT accesible:

```bash
cd /home/cetel/FPV_DETECTOR_TURNKEY
python3 scripts/instalar.py --check
python3 scripts/instalar.py --prepare
```

Resuelve los avisos de `--check` antes de ejecutar `--prepare`. La preparación compila el decodificador C++ incluido, instala las fuentes finales y crea `.venv-tft` con las dependencias registradas. Al terminar inicia **automáticamente la calibración de cuatro cruces** en la TFT. Toca las cruces siguiendo las indicaciones; el resultado se guarda en `/home/cetel/detector_drone/fpv_touch_calibration.json` y pertenece únicamente a esa pantalla.

Si falla solo la calibración, conserva la instalación preparada y vuelve a calibrar:

```bash
cd /home/cetel/FPV_DETECTOR_TURNKEY
python3 scripts/calibracion_touch_turnkey.py
python3 scripts/instalar.py --check
```

Usa `python3 scripts/calibracion_touch_turnkey.py --manual` si T_IRQ/GPIO4 no responde. No vuelvas a ejecutar `--prepare` sobre el destino existente: el instalador solo admite instalaciones nuevas. `--activate` exige una calibración válida.

### 5. Comprobar el detector y activar el arranque automático

Con el transmisor de prueba apagado durante la calibración inicial:

```bash
cd /home/cetel/detector_drone
./iniciar_detector_drone_v19e_consolidado.sh
```

Después de calibrar, usa una señal de ensayo autorizada para comprobar `SCAN → RF persistente → VIDEO → SCAN`, imagen TFT, touch y audio. Puedes revisar `/dev/shm/display_tft_v19e_consolidado.log` desde otra sesión SSH. Los resultados de rendimiento del prototipo son una referencia; verifica el funcionamiento en esta nueva unidad.

Detén el ensayo con `q` y comprueba que ningún otro proceso utiliza HackRF. Activa los servicios:

```bash
cd /home/cetel/FPV_DETECTOR_TURNKEY
python3 scripts/instalar.py --activate
```

Sigue la confirmación del instalador. La activación instala los servicios systemd del detector, pantalla de arranque, web, TAK y volumen USB. Comprueba el estado y reinicia para validar el arranque automático:

```bash
sudo systemctl status detector-fpv fpv-web-tft fpv-tak-sensor
bash scripts/verificar.sh
sudo reboot
```

Tras reconectar, vuelve a comprobar los servicios y abre `http://<IP_DE_LA_PI>:8765` desde la LAN. Para conectar con TAK, ejecuta `fpv-tak` y configura los certificados propios de la nueva unidad, servidor, callsign y posición GNSS o fija. No copies certificados ni calibraciones de otra Pi.

## Comandos de operación

```bash
fpv                        # tmux: Ctrl+B, D para desconectar sin parar
fpv-tak                    # TUI: nombre, SoftCert --legacy, IP/DHCP, GNSS, servidor, TTL
fpv-tak-softcert ~/Auto_Cert_DS4.zip [--legacy]
python3 ~/detector_drone/configurar_posicion_v32.py status
sudo systemctl status detector-fpv fpv-web-tft fpv-tak-sensor
bash /home/cetel/FPV_DETECTOR_TURNKEY/scripts/verificar.sh
sudo shutdown -h now
```

Web local (sin contraseña): `http://<IP_DE_LA_PI>:8765` — **solo LAN confiable**, nunca publicar el puerto en Internet.

## Navegación

- [`AGENTS.md`](AGENTS.md): contrato técnico para Codex; cambios y validación.
- [`docs/INSTALACION.md`](docs/INSTALACION.md): wiring, preparación de hardware, dependencias y servicios.
- [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md): flujos IPC, estado y controles, GNSS y CoT.
- [`docs/REPRODUCIBILIDAD.md`](docs/REPRODUCIBILIDAD.md): orígenes, C++ externo, hashes, baseline, Git.
- [`docs/VERIFICACION.md`](docs/VERIFICACION.md): pruebas y criterios de aceptación.
- [`docs/SEGURIDAD.md`](docs/SEGURIDAD.md): restricciones, certificados y eventos TAK.
- [`CHANGELOG.md`](CHANGELOG.md): historial consolidado.

