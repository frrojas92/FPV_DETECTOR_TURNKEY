# FPV DETECTOR — Release de referencia para Raspberry Pi 5

**Versión:** `v1.0.0-reference` · **fecha:** 2026-10-14 · **estado:** fuentes Python finales integradas; requiere captura única del decodificador C++ validado para ser autónomo y una prueba en la segunda Pi para afirmar reproducibilidad de hardware.

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

## Inicio rápido — dos etapas

### A. UNA VEZ en la Raspberry Pi que ya funciona

Copia el ZIP de este repositorio a `/home/cetel/`, descomprímelo y entra en la carpeta.

```bash
cd ~/FPV_DETECTOR_TURNKEY_V1_0
bash scripts/capturar_referencia.sh
python3 -m unittest discover -s tests -v
python3 scripts/verificar_repo.py
```

Eso incorpora a `vendor/fpvdec/` el **código fuente C++ FPS30 completo y modificado que ya está validado**; también crea `reference/requirements.lock.txt` y manifiestos de versiones, utilizando el `.venv-tft` del prototipo. **No** incluye certificados, llaves, IP o coordenadas privadas. Inspecciona licencias, archivos y `git diff` antes de subirlos a GitHub. Si falta la fuente C++ o la licencia, se detiene.

### B. En la segunda Raspberry Pi 5 (Debian 13 aarch64)

Instala el sistema operativo, configura SPI, cablea la TFT y conecta HackRF y audio USB. Clona el repositorio **después** de la captura/commit de la etapa A.

```bash
cd ~/FPV_DETECTOR_TURNKEY_V1_0
bash scripts/dependencias_debian13.sh
python3 scripts/instalar.py --check
python3 scripts/instalar.py --prepare
```

`--prepare` compila `fpvdec` desde el fuente final integrado (sin aplicar parches), instala el código y crea el entorno `.venv-tft` si no existe. Preserva la configuración TAK y registra respaldo privado de archivos sustituidos. Si faltan dependencias o el código C++ capturado, falla antes de activar servicios.

Probar **manualmente** con VTX apagado durante la calibración y después con señal autorizada:

```bash
cd ~/detector_drone
./iniciar_detector_drone_v19e_consolidado.sh
# En otra sesión SSH:
# revisar /dev/shm/display_tft_v19e_consolidado.log
```

Con el ensayo manual detenido (`q`) y sin otro proceso HackRF:

```bash
cd ~/FPV_DETECTOR_TURNKEY_V1_0
python3 scripts/instalar.py --activate
```

`--activate` solicita confirmación `ACTIVAR`, respalda unidades `systemd`, retira **solo** drop-ins históricos conocidos y registra servicios finales directos, sin depender de V2/V3/V3.3 anteriores.

## Comandos de operación

```bash
fpv                        # tmux: Ctrl+B, D para desconectar sin parar
fpv-tak                    # TUI: nombre, SoftCert --legacy, IP/DHCP, GNSS, servidor, TTL
fpv-tak-softcert ~/Auto_Cert_DS4.zip [--legacy]
python3 ~/detector_drone/configurar_posicion_v32.py status
sudo systemctl status detector-fpv fpv-web-tft fpv-tak-sensor
bash scripts/verificar.sh
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

## Limitación explícita de este ZIP

Los paquetes anteriores de la conversación contenían el módulo de inserción C++ V19-E y scripts de parcheo, **no el árbol C++ final completo** del `fpvdec` ya compilado en tu Raspberry Pi ni su entorno SDL2 específico. Por eso no se inventa ni se reconstruye silenciosamente ese código. `scripts/capturar_referencia.sh` es el único paso adicional necesario para completar la **fuente final** en Git. Una segunda instalación física todavía debe validarse; esta release es una referencia técnica consolidada, no una certificación de replicación ya realizada.

<!-- TURNKEY-RC2-AUTOCALIB -->
## Primer arranque Turnkey RC2 — Raspberry Pi 5, Debian 13 Trixie

Estos son los **tres ajustes requeridos por las pruebas de instalación en hardware nuevo**:

1. **SPI para XPT2046 CE1:** debe estar disponible `/dev/spidev0.1`. Con permisos de administrador, ejecutar `sudo bash scripts/habilitar_spi_debian13.sh` **solo si no existe**. El script respalda `/boot/firmware/config.txt`, habilita `dtparam=spi=on` y solicita reiniciar. Luego verificar `ls -l /dev/spidev0.1` y pertenencia al grupo `spi` (`id -nG`). No se cambia el boot automáticamente durante `--prepare` porque requiere reinicio.
2. **Python TFT:** `--activate` admite los enlaces simbólicos normales de `python3 -m venv`, y verifica que `sys.prefix` corresponda al `.venv-tft` preparado; ya no rechaza un `bin/python` válido solo por ser un symlink.
3. **Touch XPT2046:** al terminar exitosamente `python3 scripts/instalar.py --prepare`, se inicia **automáticamente** la calibración de cuatro cruces en la TFT, sin un segundo comando. Guarda `~/detector_drone/fpv_touch_calibration.json`. `--activate` exige calibración válida antes de iniciar servicios. El operador puede recalibrar luego con `python3 scripts/calibracion_touch_turnkey.py`, o `--manual` si T_IRQ/GPIO4 no responde.

**Importante:** ejecutar `--prepare` con un terminal SSH interactivo y la TFT disponible. Si la calibración falla, la preparación permanece íntegra para diagnóstico: no borres `~/detector_drone`, vuelve a ejecutar `python3 scripts/calibracion_touch_turnkey.py` y, tras validar `--check`, ejecuta `--activate`. No se admite ejecutar `--prepare` por segunda vez sobre el destino existente.

El archivo `fpv_touch_calibration.json` es propio de cada pantalla. No se incluye en Git ni en el ZIP de distribución; la calibración por defecto NO se copia del equipo original. Cuando el detector está activo, detén previamente los servicios que utilicen la TFT antes de recalibrar.

