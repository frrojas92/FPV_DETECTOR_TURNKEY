# Arquitectura de referencia

```text
USB HackRF One (único SDR)
  └── fpv58_detector19e.py (SCAN / VIDEO manual)
       ├── hackrf_sweep (SCAN)
       ├── fpvdec C++ V19-E FPS30 (VIDEO, 10MSPS, NTSC RGB24)
       │    └── /dev/shm/fpv_color_<uid>.raw  (último fotograma solamente)
       ├── /dev/shm/fpv_status.json (estado atómico)
       ├── TUI tmux, alarma ALSA (KT USB Audio)
       ├── display_tft_v19e_fps30.py → SPI ILI9341 24MHz y touch XPT2046
       ├── fpv_web_v4.py → HTTP puerto 8765, MJPEG 30FPS, controles compartidos
       └── fpv_tak_sensor.py → GNSS gpsd/fijo → CoT TLS+SoftCert, alertas volátiles
```

- TFT/web usan el renderizador gráfico `display_tft_v19e.py` y/o su versión optimizada, sin abrir otro HackRF.
- Control del detector se comunica mediante el archivo de comandos táctiles existente; no desarrollar un segundo propietario del SDR.
- `fpv-boot-splash.service` ejecuta splash V3 una sola vez por 30 segundos y termina antes de iniciar el detector. En SPI no deben dibujar dos procesos simultáneos.
- `detector-fpv.service` usa supervisor tmux `fpv19e-tmux-supervisor` y lanzador FINAL `iniciar_detector_drone_v19e_consolidado.sh`. El lanzador original RAMTEST permanece instalado como referencia, no es el servicio activo.
- `fpv-web-tft.service` apunta a `fpv_web_v4.py --fps 30` en 0.0.0.0:8765. Restringir el segmento de red.
- `fpv-tak-sensor.service` es un sidecar Python stdlib, lee `/dev/shm/fpv_status.json`, no interviene en la recepción/demodulación.
- El sensor de TAK tiene UID único por unidad. Su marcador CoT representa la posición del sensor y cobertura azimutal de 360°. No realiza radiogoniometría.
- Solo dos alertas visibles: `-RF-VISUAL-RING`, `-RF-UAS-HYPOTHESIS`, TTL 12s/refresh 3s. `-RF-ALERT` puede recibir caducidad legada pero jamás un CoT activo. Evento del UAV es una hipótesis, no pista aeronáutica medida.
- SoftCert `caCert.p12`, `clientCert.p12`, `config.pref` → importación a `tak_certs/` con permisos locales, TLS verificado. El TUI permite `--legacy` PKCS#12.

## Control operacional

Durante SCAN, detectar candidatos; seleccionar canal persistente; entrar VIDEO detiene escaneo en **el único HackRF**. Salir VIDEO reanuda barrido. La latencia deseada sigue siendo <=3s, sin colas de imágenes antiguas. El TAK sidecar evita generar nuevos eventos RF desde estados antiguos o desde VIDEO.

## Qué no está en la referencia

Remote ID BLE/Wi-Fi, escaneo multibanda y conmutación/triplexor RF, geolocalización de transmisor, RTSP, triangulación, identificación hostil automática. No publicarlos como features existentes.
