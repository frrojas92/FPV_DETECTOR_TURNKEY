# CHANGELOG

## v1.0.0-reference — 2026-10-14

- Consolidación de `fpv58_detector19e.py`, RAMTEST, TFT FPS30 final, fuente Web V4, Splash V3, TMUX/systemd y TUI SSH, **sin parcheo histórico durante la instalación**.
- Perfil TFT a SPI 24 MHz, video refresh 0.025, FPS TFT 24–25 y Web V4 30 medidos en RPi5 original; backend fpvdec V19-E FPS30 fuente exacta exportable desde origen.
- TAK Sensor V3.3.1 consolidado: SoftCert ZIP automático, TLS/mTLS, GNSS, UID fijo, caducidad por cierre/pérdida de fix; modo volátil de 12 s; **solo círculo rojo y UAV OTAN provisional**; limpieza de antiguo marcador rojo RF.
- TUI TAK V3.3.2: selección interactiva de `--legacy` para certificados PKCS#12 antiguos, conservando verificación TLS estricta.
- Instalación final por servicios completos (no overlays V2/V3), preflight, inventario, pruebas y workflow de captura de prototipo.
- Limitación expresa: segunda RPi no validada; paquete inicial requiere captura local única del árbol C++ validado y dependencias para completar repositorio Git.
