# AGENTS.md — contrato para Codex y contribuidores

## Producto y plataforma obligatoria

- Esta carpeta contiene la **baseline consolidada** `v1.0.0-reference`, no un conjunto de parches históricos.
- Equipo de referencia: Raspberry Pi 5 (8 GB), Debian GNU/Linux 13 Trixie aarch64, HackRF One, TFT SPI ILI9341 2.4" 240×320, XPT2046 CE1, KT USB Audio.
- Directorio de ejecución: `/home/cetel/detector_drone`; usuario `cetel`.
- **Un solo HackRF**. El detector `fpv58_detector19e.py` es el único proceso que lo administra. El servidor web y TAK nunca crean otra sesión SDR.
- Conservar `iniciar_detector_drone_v19e_ramtest.sh` **como respaldo de fuente**; modo activo consolidado: `iniciar_detector_drone_v19e_consolidado.sh`, lanzado mediante supervisor tmux.
- SPI ILI9341 **24 MHz** (validado), `video-refresh=0.025`, última-frame-sólo, no incrementar a 32 MHz sin experimento separado y medidas.
- `fpvdec`: árbol final vendorizado `vendor/fpvdec/` después de `capturar_referencia.sh`, publicación RGB24 sin colas, **30 FPS a 10 MSPS**, no reinstalar una versión upstream sin las extensiones V19-E FPS30.
- Web V4: 30 FPS máximo MJPEG; representación de la TFT; HTTP LAN puerto 8765 **sin usuario/clave**. No exponer fuera de LAN.
- Touch calibración específica por TFT, no compartir `fpv_touch_calibration.json` en Git. Audio USB usa ALSA; persistencia física debe verificarse por cada clon.

## Reglas de TAK actuales (V3.3.2)

- Transporte TCP/TLS **mTLS** con verificación de CA y nombre del servidor, port configurable (habitual 8089). SoftCert ZIP ATAK (caCert.p12, clientCert.p12, config.pref) importa automáticamente servidor/certificados y usa conexión probada antes de aplicar cambios.
- Desde `fpv-tak` opción 1, el operador **puede elegir `--legacy`** para PKCS#12 antiguo OpenSSL3. El CLI `cargar_softcert.py ZIP --legacy` también es válido. **No usar `--legacy` para desactivar verificación TLS**; solo permite cifrados PKCS#12 antiguos.
- TUI `fpv-tak`: cambio de nombre/callsign, servidor/puerto/SNI TLS, SoftCert, posición GNSS/fija, TTL, controles de conexión, IPv4 DHCP/manual diferida si `nmcli` está disponible. **Nunca desactivar Ethernet inmediatamente desde una sesión SSH**.
- Posición de sensor: UID estable; `gpsd` opcional, posición nueva por desplazamiento >=5m y heartbeat 10s; GNSS inválido >8s provoca caducidad. No activar fallback a posición estática silenciosamente.
- Mientras `RF PERSISTENTE` con snapshot reciente en SCAN: publicar **solo dos alertas visibles**: círculo rojo UID `<sensor>-RF-VISUAL-RING` y UAV OTAN provisional UID `<sensor>-RF-UAS-HYPOTHESIS`. **NO publicar** SpotMap rojo UID `<sensor>-RF-ALERT` activo. Enviar su caducidad únicamente para limpiar visualizaciones antiguas.
- Las dos alertas son **volátiles**: cada 3 s, TTL 12 s, `geochat=false` porque su historial no caduca. Cierre ordenado expira ambos UIDs y sensor. Desconexión brutal de energía/red depende de TTL: **no prometer desaparición instantánea remota**.
- El anillo rojo es símbolo visual, **no radio de alcance ni ubicación del transmisor**. El UAV hostil OTAN es **HIPÓTESIS NO GEOLOCALIZADA**: nunca inventar coordenadas de aeronave, identidad ni intención. Mantener metadatos que advierten que las coordenadas son del SENSOR.
- No crear marcadores activos con datos viejos cuando el detector esté en VIDEO o haya perdido posición válida.

## Requisitos para cambios

1. Crear branch/PR; no modificar baseline sin pruebas y rollback. Mantener estado y comandos existentes compatibles (`/dev/shm/fpv_status.json`, touch commands, CoT GNSS).
2. Sin parches aplicados al desplegar. Editar fuentes finales en `src/` y `vendor/fpvdec/` —**después de capturar el C++ real**—; documentar versión upstream/licencias.
3. Ejecutar `python3 -m unittest discover -s tests -v`, `python3 scripts/verificar_repo.py`, `bash -n` de scripts y compilar C++ en RPi5. Añadir pruebas a todo cambio que afecte control remoto o CoT.
4. Prueba física con transmisión RF de ensayo autorizada: apagado durante calibración, luego SCAN → RF persistente → VIDEO → SCAN. Verificar lock, `dropped_bytes=0`, fps; touch y audio.
5. Ensayar pérdida GNSS, `systemctl stop fpv-tak-sensor`, interrupción TCP/TLS y caducidad en ATAK; comprobar que el **punto RF rojo no reaparece**.
6. Nunca incluir claves `.p12`, `.key`, `tak_certs`, SoftCert ZIP, `fpv_tak_config.json`, IP/posición de una unidad real, tokens o respaldos privados en Git. `config.example.json` no puede tener secretos.
7. No insertar dependencia de Remote ID, multibanda 1.2/3.3 GHz, streaming RTSP o sensor de dirección como si fueran características probadas: están **pendientes**.
8. Compatibilidad de instalación: archivos completos, copia de seguridad del destino, revisión de servicios systemd y overrides. No alterar la Pi original sin consentimiento operacional.

## Reproducibilidad — bloqueo necesario

El ZIP inicial no contiene el repositorio C++ final previamente compilado ni el pip lock de la Pi real. Antes de declarar un clon reproducible y antes del primer commit final:

```bash
bash scripts/capturar_referencia.sh
python3 scripts/verificar_repo.py
```

Este script extrae el árbol final `v19e_fps30_experimental` desde la **Pi que funciona** y registra hashes. Revisa el LICENSE MIT del upstream y su relación con el árbol capturado. Al instalar un clon, `scripts/instalar.py --check` y `--prepare` abortan si no existe la fuente completa. Un build exitoso es necesario pero no suficiente: registrar pruebas físicas de la segunda unidad.

## Prioridades próximas

Reproducibilidad real desde Debian 13 limpio; pruebas CI y smoke de hardware; documentación de drivers TFT; integración opcional Remote ID; expansión multibanda con RF selectiva y aislamiento. Preservar rendimiento antes de añadir nuevos subsistemas.
