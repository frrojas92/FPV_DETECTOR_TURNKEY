#!/usr/bin/env python3
"""Static consistency and secret checks for baseline release; no hardware required."""
from __future__ import annotations
import argparse, ast, hashlib, json, re, subprocess, sys
from pathlib import Path
R=Path(__file__).resolve().parents[1]
PYFILES=list((R/'src').rglob('*.py'))+list((R/'scripts').rglob('*.py'))+list((R/'tests').glob('*.py'))
def verify(ci=False):
    issues=[]; warnings=[]
    for f in PYFILES:
        try:ast.parse(f.read_text(encoding='utf8'),filename=str(f))
        except SyntaxError as e:issues.append(f'Sintaxis {f.relative_to(R)}: {e}')
    for f in list((R/'scripts').rglob('*.sh'))+list((R/'src').rglob('*.sh'))+list((R/'src/ssh').glob('fpv*'))+list((R/'systemd').glob('*.sh'))+list((R/'src/detector').glob('*.sh')):
        cp=subprocess.run(['bash','-n',str(f)],capture_output=True,text=True)
        if cp.returncode:issues.append(f'Bash {f.relative_to(R)}: {cp.stderr}')
    src=(R/'src/tak/fpv_tak_sensor.py').read_text()
    tui=(R/'src/tak/fpv_tak_tui.py').read_text()
    importer=(R/'src/tak/cargar_softcert.py').read_text()
    launcher=(R/'src/detector/iniciar_detector_drone_v19e_consolidado.sh').read_text()
    must=[('TAK dos alertas', 'warning_ring_cot(cfg, loc, region' in src and 'adversary_uas_cot(cfg, loc, region' in src),
          ('No reactivar spot legacy', all('expire=True' in call for call in re.findall(r'(?<!def )\bspot_cot\(([^)]*)\)',src))),
          ('TUI --legacy', "cmd.append('--legacy')" in tui),
          ('Importador --legacy', "ap.add_argument('--legacy'" in importer),
          ('TTL de 12 segundos', '"alert_stale_s": 12' in (R/'src/tak/config.example.json').read_text()),
          ('SPI 24 MHz', (R/'src/detector/spi_hz.txt').read_text().strip()=='24000000'),
          ('FPS30 ejecutable consolidado', 'build-fps30/fpvdec' in launcher),
          ('Web 30FPS systemd', '--fps 30' in (R/'systemd/fpv-web-tft.service').read_text()),
          ('Sin auth HTTP Basic', 'WWW-Authenticate' not in (R/'src/web/fpv_web_v4.py').read_text())]
    for label,yes in must:
        if not yes:issues.append(label)
    if not (R/'vendor/fpvdec/src/main.cpp').exists():
        (issues if ci else warnings).append('Fuente final fpvdec no capturada: ejecutar scripts/capturar_referencia.sh en la RPi validada')
    if not (R/'reference/requirements.lock.txt').exists():
        (issues if ci else warnings).append('Lock de Python aún no capturado desde la Pi validada')
    for f in R.rglob('*'):
        if not f.is_file() or any(x in f.parts for x in ('.git','__pycache__')):continue
        if f.suffix.lower() in ('.p12','.pfx','.key','.pref'):
            issues.append('ARCHIVO SENSIBLE en repositorio: '+str(f.relative_to(R)))
        if f.name in ('fpv_tak_config.json','fpv_touch_calibration.json','Auto_Cert_DS4.zip'):
            issues.append('CONFIG PRIVADA en repositorio: '+str(f.relative_to(R)))
    for line in warnings:print('PENDIENTE:',line)
    for line in issues:print('ERROR:',line)
    print('PYFILES=',len(PYFILES),'ERRORES=',len(issues),'PENDIENTES=',len(warnings))
    return 1 if issues else 0
if __name__=='__main__':
    a=argparse.ArgumentParser();a.add_argument('--ci',action='store_true');x=a.parse_args();sys.exit(verify(x.ci))
