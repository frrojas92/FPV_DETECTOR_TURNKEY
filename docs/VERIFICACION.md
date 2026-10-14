# Plan de pruebas

## Fuera de Raspberry Pi

```bash
python3 -m unittest discover -s tests -v
python3 scripts/verificar_repo.py
bash -n scripts/capturar_referencia.sh scripts/dependencias_debian13.sh scripts/verificar.sh
```

Las pruebas incluyen: certificado TLS cliente-servidor sintético, GNSS/fix caducado, UID estable, CoT `OFFLINE`, alerta volátil, prohibición del marcador SpotMap RF activo, icono UAV hipotético y sintaxis del argumento `--legacy` en el TUI. No se utilizan claves privadas de un TAK Server real.

## En Raspberry Pi

```bash
bash scripts/verificar.sh
sudo systemctl status fpv-boot-splash detector-fpv fpv-web-tft fpv-tak-sensor --no-pager
journalctl -b -u detector-fpv -u fpv-boot-splash -n 80 --no-pager
cat /dev/shm/fpv_tak_status.json
vcgencmd get_throttled
vcgencmd measure_temp
```

Usar una transmisión FPV analógica propia/autorizada para obtener valores comparables: `published` (30fps), `TFT` (24–25), Web V4 (30fps), `dropped_bytes=0`, `ring` bajo. Esas cifras proceden del prototipo original, no son umbrales garantizados para cada réplica. En WEB consultar indicador del navegador, no el `/api/health` de una conexión HTTP recién creada.

## TAK / TLS y posición

Abrir ATAK en red local confiable; cargar SoftCert mediante TUI (con `--legacy` solo si necesario). Confirmar presencia del sensor en posición válida, luego la aparición de **solo círculo rojo + OTAN provisional** durante RF persistente. NO punto rojo RF. En modo VIDEO no deben crearse alertas RF nuevas por estados viejos. Interrumpir conexión y medir desaparición por TTL (12 s tras último refresco, sujeto a cliente/servidor). Detener servicio ordenadamente y revisar expiración inmediata por UID cuando mTLS aún esté conectada. Verificar pérdida GNSS.

## Criterios de no-regresión

- Una sola sesión HackRF; nada genera cola de frames atrasados ni modifica la frecuencia/calibración unilateralmente.
- Servicio TAK funciona con TUI y SoftCert; `--legacy` no desactiva validación TLS.
- TFT y Web no se ralentizan perceptiblemente, y SPI sigue a 24 MHz.
- Ningún fichero con claves o direcciones privadas aparece en `git status` ni release.
