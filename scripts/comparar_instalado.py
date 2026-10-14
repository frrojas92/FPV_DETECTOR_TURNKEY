#!/usr/bin/env python3
"""Compare the final repository source with actual files installed on the working Pi.

No private files are opened. This command is read-only and does not auto-replace
source files, because differences must be reviewed and integrated intentionally.
"""
from __future__ import annotations
from pathlib import Path
import hashlib, json, os
ROOT=Path(__file__).resolve().parents[1]
PROJECT=Path.home()/'detector_drone'
PATHS={
 'src/detector/fpv58_detector19e.py':'fpv58_detector19e.py',
 'src/detector/fpv_color_transport_v182.py':'fpv_color_transport_v182.py',
 'src/detector/fpv_spectrum_v17.py':'fpv_spectrum_v17.py',
 'src/detector/fpv_video_transport_v181.py':'fpv_video_transport_v181.py',
 'src/detector/iniciar_detector_drone_v19e_ramtest.sh':'iniciar_detector_drone_v19e_ramtest.sh',
 'src/detector/iniciar_detector_drone_v19e_consolidado.sh':'iniciar_detector_drone_v19e_consolidado.sh',
 'src/tft/display_tft_v19e.py':'display_tft_v19e.py',
 'src/tft/display_tft_v19e_fps30.py':'display_tft_v19e_fps30.py',
 'src/web/fpv_web_v4.py':'fpv_web_v4.py',
 'src/splash/fpv_boot_splash_v3.py':'fpv_boot_splash_v3.py',
 'src/tak/fpv_tak_sensor.py':'fpv_tak_sensor.py',
 'src/tak/fpv_tak_tui.py':'fpv_tak_tui.py',
 'src/tak/cargar_softcert.py':'cargar_softcert.py',
}
def h(f):return hashlib.sha256(f.read_bytes()).hexdigest()
def main():
    report={}
    for rep,inst in PATHS.items():
        source=ROOT/rep; live=PROJECT/inst
        if not source.exists(): continue
        if not live.exists():status='NO_INSTALADO';observed=None
        else:
            observed=h(live);status='IGUAL' if observed==h(source) else 'DIFERENTE'
        report[rep]={'instalado':inst,'status':status,'release_sha256':h(source),'installed_sha256':observed}
        print(f'{status:13} {rep}')
    out=ROOT/'reference/runtime_hashes.json'
    out.write_text(json.dumps(report,indent=2)+'\n')
    print('Informe SHA sin contenido sensible:',out)
    print('DIFERENTE en TAK TUI puede ser esperado: el repo agrega --legacy que aún no se instaló.')
    print('Otras diferencias deben inspeccionarse ANTES de afirmar reproducción exacta.')
if __name__=='__main__':main()
