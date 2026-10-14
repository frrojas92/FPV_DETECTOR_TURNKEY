# Reproducibilidad y cadena de procedencia

## Qué contiene este ZIP sin ejecutar acciones adicionales

1. **Código Python integrado y final** del detector V19-E, TFT original y TFT FPS30 optimizada, Web V4, splash V3, sensor TAK V3.3.1, TUI V3.3.2 con opción `--legacy`, SoftCert/gpsd y servicios completos de systemd. El código se puede leer y versionar ahora.
2. **No contiene aún** el árbol C++ original y modificado ya compilado de `fpvdec`, el archivo de versiones pip congeladas de la Pi validada ni una dependencia SDL2 privada exacta. Esos materiales nunca estaban en los ZIP originales; no generar código ficticio para rellenarlos.
3. La captura toma el árbol completo existente en la Pi: `~/detector_drone/v19e_fps30_experimental/`, incluida la fuente C++ de FPS30 y la licencia MIT proveniente del proyecto externo `zubon2003/5G8atv-rf-hackrf-decoder` cuando está presente en el checkout. Fuente pública de referencia: https://github.com/zubon2003/5G8atv-rf-hackrf-decoder ; confirmar contra el remoto real de la Pi.
4. El script de captura genera el lock de Python, un listado apt y hashes para comparar implementaciones; las versiones/kernel/hardware pueden hacer que el mismo build no sea bit a bit idéntico. No afirmar reproducibilidad de binario sin medición.

## Captura única

```bash
cd ~/FPV_DETECTOR_TURNKEY_V1_0
bash scripts/capturar_referencia.sh
python3 scripts/verificar_repo.py --ci
python3 -m unittest discover -s tests -v
sha256sum vendor/fpvdec/src/main.cpp
```

Antes de publicar un repositorio externo, revisar `vendor/fpvdec` y la licencia del código; no incorporar archivos derivados sin el aviso de licencia, no subir configuraciones privadas ni archivos de IQ/video capturados de terceros. Revisar `reference/requirements.lock.txt` y `reference/apt-versions.txt` antes del commit.

## Crear GitHub (el usuario controla el remoto)

```bash
git init
 git add README.md AGENTS.md CHANGELOG.md docs/ src/ scripts/ systemd/ tests/ reference/ vendor/ .gitignore .github/
 git commit -m 'baseline: detector FPV V19E FPS30 SPI24 Web30 TAK V3.3.2'
 git tag -a v1.0.0-reference -m 'Raspberry Pi 5 baseline validada, clon físico pendiente'
# Configurar 'git remote add origin ...' a tu repo GitHub y hacer push.
```

La cuenta/URL del remoto no está precargada. GitHub Actions ejecuta pruebas estáticas y TAK; pruebas en hardware real son obligatorias antes de afirmar réplica satisfactoria. Usa ramas de desarrollo y deja inmutable la etiqueta `v1.0.0-reference`.

## Ensayo de clon a hardware real

- Registrar versión OS/kernel/firmware, paquetes apt, `pip list`, hash fuente/backend, estado `ldd fpvdec`, wiring, consumo/carga, CPU/GPU/temp.
- VTX apagado al calibrar, luego RF persistente y selección VIDEO.
- FPS backend cerca de 30; TFT observada 24–25 a SPI24MHz; Web V4 cerca de 30; `dropped_bytes=0`, `ring≈0`, baja latencia.
- Comprobar touchscreen (recalibrar si necesario), salida ALSA al 100% cuando haya controles, arranque Splash V3 y SSH/TUI.
- Verificar CoT sensor en TAK; importar SoftCert; probar host/CA TLS, reconexión, GPS/fijo, dos marcadores activos, TTL 12 s al cortar red, caducidad inmediata al detener servicio ordenadamente.
- Registrar los resultados en un informe fechado, **sin credenciales ni coordenadas precisas**. Solo entonces declarar la réplica verificada.

## Comprobación de divergencias del código instalado

`bash scripts/capturar_referencia.sh` ejecuta también `scripts/comparar_instalado.py`. El archivo `reference/runtime_hashes.json` compara SHA-256 de la release y de los scripts efectivamente instalados, sin copiar secretos ni coordenadas. Una diferencia de hash puede ser legítima (p.ej. el TUI `--legacy` todavía no instalado en el prototipo). **Toda divergencia en detector, TFT o Web requiere revisión de código y una prueba antes de marcar la release como plenamente reproducible.** No se sobreescribe automáticamente el código consolidado con una copia de campo.
