#!/usr/bin/env bash
# Run on the validated Raspberry Pi 5; commits NO private config/certs.
set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="${FPV_PROJECT:-$HOME/detector_drone}"
SRC="$PROJECT/v19e_fps30_experimental"
[[ "$(id -un)" == cetel ]] || { echo 'Ejecutar como usuario cetel' >&2;exit 2; }
[[ -f "$SRC/src/main.cpp" && -f "$SRC/CMakeLists.txt" ]] || { echo "Falta fuente FPS30: $SRC" >&2;exit 2; }
grep -q 'FPV_FPS30_EXPERIMENTAL' "$SRC/src/main.cpp" || { echo 'No es la fuente FPS30 validada (falta marcador).';exit 2; }
python3 - "$SRC" "$HERE/vendor/fpvdec" "$PROJECT" "$HERE/reference" "$HERE/assets" <<'PY'
import sys,os,shutil,subprocess,json,hashlib,re
from pathlib import Path
src,dest,proj,ref,assets=map(Path,sys.argv[1:])
if any(dest.iterdir()) and any(x.name!='README.md' for x in dest.iterdir()):
    raise SystemExit('vendor/fpvdec YA contiene fuente: no sobrescribir. Comparar primero.')
for p in src.rglob('*'):
    if p.is_symlink():raise SystemExit('Fuente con enlaces simbólicos no esperados: '+str(p))
for p in src.rglob('*'):
    rel=p.relative_to(src)
    if any(n in ('.git','__pycache__','build','build-fps30','build-v19e') or n.startswith('build-') for n in rel.parts):continue
    if p.is_file() and (p.suffix in ('.cs8','.raw','.iq','.ppm','.png','.mp4','.log','.o','.a','.so') or p.stat().st_size>8_000_000):continue
    target=dest/rel
    if p.is_dir():target.mkdir(parents=True,exist_ok=True)
    elif p.is_file():
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
# Credit/license: if missing on patched source, recover license from upstream checkout.
if not (dest/'LICENSE').exists():
    upstream=proj/'5G8atv-rf-hackrf-decoder'/'LICENSE'
    if not upstream.is_file():raise SystemExit('FALTA licencia upstream LICENSE; no publicar hasta obtenerla')
    shutil.copy2(upstream,dest/'LICENSE')
if not (dest/'src/main.cpp').is_file():raise SystemExit('Falta main.cpp')
assert 'FPV_FPS30_EXPERIMENTAL' in (dest/'src/main.cpp').read_text()
ref.mkdir(parents=True,exist_ok=True)
# Dependency versions from the reference Pi, restricted to package==version; never URLs or credentials.
venv=proj/'.venv-tft/bin/python'
if not venv.is_file():raise SystemExit('Falta entorno Python TFT')
p=subprocess.run([str(venv),'-m','pip','list','--format=json'],capture_output=True,text=True,check=True)
packs=json.loads(p.stdout)
lock=[]
for x in packs:
    name,version=x['name'],x['version']
    if not re.fullmatch('[A-Za-z0-9_.-]+',name) or not re.fullmatch('[A-Za-z0-9_.+!-]+',version):
        raise SystemExit('Versión de paquete no estándar; revisar pip list')
    lock.append(f'{name}=={version}')
(ref/'requirements.lock.txt').write_text('\n'.join(sorted(lock,key=str.lower))+'\n')
# Versions don't contain IPs, but still review before publication.
packages=subprocess.run(['dpkg-query','-W','-f=${binary:Package}==${Version}\\n'],capture_output=True,text=True,check=False)
(ref/'apt-versions.txt').write_text(packages.stdout)
(proj/'warning.wav').is_file() and shutil.copy2(proj/'warning.wav',assets/'warning.wav')
manifest={
 'source_tree':'v19e_fps30_experimental',
 'source_sha256':hashlib.sha256((dest/'src/main.cpp').read_bytes()).hexdigest(),
 'source_license':'vendor/fpvdec/LICENSE',
 'tft_installed_sha256': hashlib.sha256((proj/'display_tft_v19e_fps30.py').read_bytes()).hexdigest() if (proj/'display_tft_v19e_fps30.py').exists() else None,
 'decoder_binary_sha256':hashlib.sha256((src/'build-fps30/fpvdec').read_bytes()).hexdigest() if (src/'build-fps30/fpvdec').exists() else None,
 'pi_python':subprocess.run([str(venv),'--version'],capture_output=True,text=True).stdout.strip(),
}
(ref/'reference-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print('Fuente fpvdec final y dependencias capturadas en vendor/ y reference/.')
print('REVISAR antes de git add: LICENSE, git diff, licencia/cambios del upstream, requirements.lock, apt-versions.')
print('Los datos privados (SoftCert, IP, posiciones, calibración, secretos) NO se exportaron.')
PY

python3 "$HERE/scripts/comparar_instalado.py"
