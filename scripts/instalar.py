#!/usr/bin/env python3
"""Install a single FPV release (not a chain of historical patches).

--check: review prereqs without modifications.
--prepare: assemble final files, compile the captured full C++ source, preserve old config.
--activate: replace controlled systemd services after --prepare and explicit confirmation.
"""
from __future__ import annotations
import argparse, datetime as dt, fcntl, hashlib, json, os, pwd, shutil, subprocess, sys, uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PROJECT=Path.home()/'detector_drone'
REQUIRED=['src/detector/fpv58_detector19e.py','src/tft/display_tft_v19e_fps30.py',
          'src/web/fpv_web_v4.py','src/tak/fpv_tak_sensor.py','src/tak/fpv_tak_tui.py',
          'src/splash/fpv_boot_splash_v3.py']
SVC_NAMES=['fpv-boot-splash','detector-fpv','fpv-web-tft','fpv-tak-sensor','fpv-usb-volume']
KNOWN_DROPINS={
 'fpv-boot-splash': {'95-fpv-splash-v2.conf','98-fpv-splash-v3.conf'},
 'detector-fpv': {'95-fpv-detector-v2.conf','80-fpv-fps30.conf'},
 'fpv-tak-sensor': {'20-graceful-shutdown-v32.conf'},
}


def call(args,**kwargs):
    print('+',' '.join(map(str,args)),flush=True)
    return subprocess.run(list(map(str,args)),check=True,**kwargs)

def need_user():
    if os.geteuid()==0 or pwd.getpwuid(os.geteuid()).pw_name != 'cetel':
        raise RuntimeError('Ejecuta como usuario cetel sin sudo; se te pedirá sudo cuando corresponda.')

def refuse_symlink(p):
    if p.is_symlink(): raise RuntimeError(f'No se modifica symlink: {p}')

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def check():
    issues=[]
    if sys.platform!='linux' or os.uname().machine!='aarch64':issues.append('Solo validado en Debian 13 aarch64 (RPi5).')
    if not (Path('/dev/spidev0.1').exists()):issues.append('SPI CE1 /dev/spidev0.1 aún no configurado.')
    final=ROOT/'vendor/fpvdec/src/main.cpp'
    if not final.is_file() or 'FPV_FPS30_EXPERIMENTAL' not in final.read_text(errors='ignore'):
        issues.append('FALTA fuente C++ FPS30 final. En RPi origen: bash scripts/capturar_referencia.sh')
    if not (ROOT/'vendor/fpvdec/CMakeLists.txt').exists():issues.append('FALTA CMakeLists.txt de fpvdec.')
    if not (ROOT/'vendor/fpvdec/LICENSE').exists():issues.append('FALTA LICENSE upstream de fpvdec.')
    if not (ROOT/'reference/requirements.lock.txt').exists():issues.append('FALTA lock Python del equipo validado: ejecutar captura.')
    for cmd in ('cmake','g++','python3','tmux','hackrf_sweep','sudo'):
        if not shutil.which(cmd):issues.append(f'Falta comando: {cmd}')
    for file in REQUIRED:
        if not (ROOT/file).is_file():issues.append('Falta archivo integral: '+file)
    print('Release:',ROOT)
    print('Proyecto destino:',PROJECT)
    print('Dependencias/avisos:')
    for msg in issues:print(' -',msg)
    if not issues: print('OK: fuente, lock y herramientas básicas encontradas (hardware físico aún requiere pruebas).')
    return issues

def backup_file(src, backup):
    refuse_symlink(src)
    if src.exists():
        name=src.name
        target=backup/name
        if target.exists():target=backup/(name+'.duplicate')
        shutil.copy2(src,target)
        return True
    return False

def stage_copy(src,dst,backup):
    refuse_symlink(dst)
    dst.parent.mkdir(parents=True,exist_ok=True)
    if dst.is_file() and sha(src)==sha(dst):return
    backup_file(dst,backup)
    shutil.copy2(src,dst)
    print('INSTALADO:',dst.relative_to(PROJECT) if PROJECT in dst.parents else dst)

def refuse_running_services():
    """Do not prepare over the operational FPV installation."""
    running = []
    for service in ("detector-fpv", "fpv-tak-sensor", "fpv-web-tft"):
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", service],
            check=False
        )
        if result.returncode == 0:
            running.append(service)
    if running:
        raise RuntimeError(
            "PREPARACIÓN BLOQUEADA: servicios activos: "
            + ", ".join(running)
            + ". No modificar el Consolidado operativo."
        )


def build_decoder(generated):
    """Install an already compiled FPS30 binary atomically."""
    import tempfile
    import os

    generated = Path(generated)

    if generated.is_symlink() or not generated.is_file():
        raise RuntimeError("Binario FPS30 precompilado inválido")

    expected_hash = sha(generated)

    build = PROJECT / "v19e_fps30_experimental/build-fps30"
    build.mkdir(parents=True, exist_ok=True)

    binary = build / "fpvdec"
    refuse_symlink(binary)

    if binary.exists():
        backup_dir = PROJECT / "backups_turnkey" / "fpvdec"
        backup_dir.mkdir(mode=0o700, parents=True)

        backup_file = backup_dir / (
            "fpvdec-" +
            dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        )

        shutil.copy2(binary, backup_file)

        if sha(binary) != sha(backup_file):
            raise RuntimeError("Respaldo fpvdec incorrecto")

    fd, name = tempfile.mkstemp(
        prefix=".fpvdec-new-",
        dir=str(build)
    )

    temporary = Path(name)

    try:
        with os.fdopen(fd, "wb") as output:
            with generated.open("rb") as source:
                shutil.copyfileobj(source, output)

            output.flush()
            os.fsync(output.fileno())

        if sha(temporary) != expected_hash:
            raise RuntimeError("SHA256 temporal incorrecto")

        temporary.chmod(0o755)
        os.replace(temporary, binary)

    finally:
        temporary.unlink(missing_ok=True)

    if sha(binary) != expected_hash:
        raise RuntimeError("SHA256 instalado incorrecto")

    print("FPS30 instalado:", binary)
    print("SHA256:", expected_hash)


def prebuild_decoder(staging):
    """Build FPS30 once and return the verified binary path."""
    staging = Path(staging)

    script = ROOT / "scripts/compilar_fpvdec_portable.sh"

    if not script.is_file():
        raise RuntimeError("Falta compilador portable FPS30")

    if staging.is_symlink() or not staging.is_dir():
        raise RuntimeError("Directorio de compilación inválido")

    call(["bash", str(script), str(staging)])

    binary = staging / "fpvdec"

    if binary.is_symlink() or not binary.is_file():
        raise RuntimeError("Compilación FPS30 incompleta")

    if not (binary.stat().st_mode & 0o111):
        raise RuntimeError("FPS30 no tiene permiso de ejecución")

    print("FPS30 precompilado:", sha(binary))

    return binary


def ensure_venv():
    env = PROJECT / ".venv-tft"
    py = env / "bin/python"

    if py.exists():
        print("Entorno TFT existente: se conserva intacto:", py)
        return

    requirements = ROOT / "reference/requirements-venv.txt"

    if not requirements.is_file():
        raise RuntimeError(
            "Falta reference/requirements-venv.txt"
        )

    call([
        "python3", "-m", "venv",
        "--system-site-packages", str(env)
    ])

    call([
        str(py), "-m", "pip", "install",
        "--requirement", str(requirements)
    ])

    print("Entorno TFT creado:", py)

def quarantine_incomplete_project(project, identity):
    """Preserve a newly created incomplete installation."""
    import uuid

    project = Path(project)

    if project.is_symlink() or not project.is_dir():
        raise RuntimeError("Destino de recuperación inválido")

    current = project.stat()

    if (current.st_dev, current.st_ino) != identity:
        raise RuntimeError(
            "El directorio cambió desde su creación; no se moverá"
        )

    quarantine = project.with_name(
        f".{project.name}.turnkey-incomplete-{uuid.uuid4().hex}"
    )

    if quarantine.exists() or quarantine.is_symlink():
        raise RuntimeError("La ruta de cuarentena ya existe")

    project.rename(quarantine)
    return quarantine


def post_prepare_touch():
    """Launch four-point XPT2046 calibration after a successful preparation.

    Failures leave a prepared installation for retry; never quarantine it.
    No services are enabled/started here.
    """
    wizard = ROOT / "scripts/calibracion_touch_turnkey.py"
    if wizard.is_symlink() or not wizard.is_file():
        raise RuntimeError("Falta el asistente TOUCH RC2")
    print("TURNKEY: iniciando calibracion TOUCH al terminar --prepare", flush=True)
    subprocess.run([
        sys.executable, str(wizard), "--project", str(PROJECT), "--auto"
    ], check=True)


def prepare():
    need_user()
    refuse_running_services()

    if PROJECT.is_symlink():
        raise RuntimeError(
            "PREPARACIÓN BLOQUEADA: destino es un enlace simbólico."
        )

    if PROJECT.exists():
        raise RuntimeError(
            "PREPARACIÓN BLOQUEADA: el directorio destino "
            "ya existe. Turnkey V1.0 solo admite "
            "instalaciones nuevas."
        )

    problems=check()
    blocking=('FALTA fuente','FALTA CMakeLists','FALTA LICENSE','FALTA lock','Falta comando')
    if any(any(m.startswith(b) for b in blocking) for m in problems):
        raise RuntimeError('Preflight bloqueado: faltan fuentes/dependencias. No se alteró el proyecto.')
    import tempfile
    with tempfile.TemporaryDirectory(
        prefix="fpv-turnkey-prebuild-"
    ) as temporary:
        prebuilt = prebuild_decoder(Path(temporary))

        PROJECT.mkdir(parents=True,exist_ok=False)
        if PROJECT.is_symlink():raise RuntimeError('Proyecto symlink no permitido')
        created = PROJECT.stat()
        created_identity = (created.st_dev, created.st_ino)
        backup=PROJECT/'backups_turnkey'/dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        try:
            backup.mkdir(mode=0o700,parents=True)
            ensure_venv()
            build_decoder(prebuilt)
            for f in ('fpv58_detector19e.py','fpv_color_transport_v182.py','fpv_spectrum_v17.py','fpv_video_transport_v181.py',
                      'iniciar_detector_drone_v19e_ramtest.sh','iniciar_detector_drone_v19e_consolidado.sh'):
                if f=='iniciar_detector_drone_v19e_ramtest.sh' and (PROJECT/f).exists():
                    print('ORIGINAL RAMTEST EXISTENTE: conservado intacto:',PROJECT/f)
                else:
                    stage_copy(ROOT/'src/detector'/f,PROJECT/f,backup)
            for f in ('display_tft_v19e.py','display_tft_v19e_fps30.py'):
                stage_copy(ROOT/'src/tft'/f,PROJECT/f,backup)
            stage_copy(ROOT/'src/web/fpv_web_v4.py',PROJECT/'fpv_web_v4.py',backup)
            stage_copy(ROOT/'src/splash/fpv_boot_splash_v3.py',PROJECT/'fpv_boot_splash_v3.py',backup)
            for f in ('fpv_tak_sensor.py','fpv_tak_tui.py','cargar_softcert.py','importar_certificados.py','inspeccionar_zip.py',
                      'configurar_posicion_v32.py','configurar_tls.py','configurar.py','probar_tls.py'):
                stage_copy(ROOT/'src/tak'/f,PROJECT/f,backup)
            spi=PROJECT/'fpv_v19e_consolidado/spi_hz.txt'
            spi.parent.mkdir(parents=True,exist_ok=True)
            if spi.exists() and spi.read_text().strip()!='24000000':
                backup_file(spi,backup)
            spi.write_text('24000000\n')
            src_wav=ROOT/'assets/warning.wav'
            if src_wav.exists() and not (PROJECT/'warning.wav').exists():stage_copy(src_wav,PROJECT/'warning.wav',backup)
            cfgfile=PROJECT/'fpv_tak_config.json'
            from sys import path as sys_path
            sys_path.insert(0,str(PROJECT))
            from fpv_tak_sensor import validate
            if cfgfile.exists():
                refuse_symlink(cfgfile)
                cfg=json.loads(cfgfile.read_text())
                print('Se conserva el UID y el servidor de TAK anterior.')
            else:
                cfg=json.loads((ROOT/'src/tak/config.example.json').read_text())
                cfg['uid']='FPV-RF-'+str(uuid.uuid4())
            cfg['tak'].update(volatile_alerts=True,alert_repeat_s=3,alert_stale_s=12,geochat=False,
                              alert_visual_ring=True,adversary_uas_marker=True)
            validate(cfg)
            if cfgfile.exists():backup_file(cfgfile,backup)
            cfgfile.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n');cfgfile.chmod(0o600)
            for fn in ('iniciar_detector_drone_v19e_ramtest.sh','iniciar_detector_drone_v19e_consolidado.sh',
                       'cargar_softcert.py','fpv_tak_tui.py','fpv_tak_sensor.py'):
                (PROJECT/fn).chmod(0o755)
            print('PREPARACIÓN COMPLETA. Respaldo:',backup)
            print('ANTES de activar: revisar touch / GNSS / audio y probar manualmente con el lanzador consolidado.')
        except Exception:
            print("PREPARACIÓN INCOMPLETA: aislando archivos nuevos.", file=sys.stderr)
            try:
                quarantine = quarantine_incomplete_project(
                    PROJECT, created_identity
                )
                print("Archivos conservados en:", quarantine, file=sys.stderr)
            except Exception as recovery_error:
                print(
                    "ADVERTENCIA: no fue posible aislar el destino:",
                    recovery_error,
                    file=sys.stderr
                )
            print(
                "No se han cambiado unidades systemd ni reiniciado servicios.",
                file=sys.stderr
            )
            raise
    post_prepare_touch()

def activate():
    """Activate a fresh prepared Turnkey installation, with rollback."""
    from activacion_systemd import FreshActivation, SERVICES

    need_user()
    refuse_running_services()

    if set(SVC_NAMES) != set(SERVICES):
        raise RuntimeError("Incompatibilidad entre servicios del instalador")
    if PROJECT.is_symlink() or not PROJECT.is_dir():
        raise RuntimeError("Ejecuta --prepare en una instalación nueva")
    decoder = PROJECT / "v19e_fps30_experimental/build-fps30/fpvdec"
    if decoder.is_symlink() or not decoder.is_file():
        raise RuntimeError("No existe fpvdec preparado")
    spi = PROJECT / "fpv_v19e_consolidado/spi_hz.txt"
    if spi.is_symlink() or not spi.is_file() or spi.read_text().strip() != "24000000":
        raise RuntimeError("Se esperaba TFT SPI 24 MHz")
    import os

    venv_dir = PROJECT / ".venv-tft"
    venv = venv_dir / "bin/python"

    if not venv.is_file() or not os.access(venv, os.X_OK):
        raise RuntimeError("Entorno Python TFT ausente")

    cfg = venv_dir / "pyvenv.cfg"
    if cfg.is_symlink() or not cfg.is_file():
        raise RuntimeError("Configuración venv TFT inválida")

    result = subprocess.run(
        [
            str(venv),
            "-c",
            "import sys,pathlib; print(pathlib.Path(sys.prefix).resolve())"
        ],
        capture_output=True,
        text=True,
        check=True
    )

    if Path(result.stdout.strip()) != venv_dir.resolve():
        raise RuntimeError("Python TFT no pertenece al venv esperado")
    if not Path("/dev/spidev0.1").exists():
        raise RuntimeError("Falta SPI CE1 /dev/spidev0.1")

    # Do not stop anything; a fresh installation must have no running services.
    lock_file = PROJECT / ".fpv_v6.lock"
    if lock_file.is_symlink():
        raise RuntimeError("Enlace simbólico en bloqueo HackRF")
    with lock_file.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Detector manual en uso; cerrar antes de activar")
        finally:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            except OSError:
                pass

    # Calibration is automatically performed by --prepare.
    from calibracion_touch_turnkey import check_calibration
    if not check_calibration(PROJECT / "fpv_touch_calibration.json"):
        raise RuntimeError("TOUCH sin calibrar; ejecuta scripts/calibracion_touch_turnkey.py")
    answer = input("Instalar y activar los servicios FPV nuevos. Escribe ACTIVAR: ").strip()
    if answer != "ACTIVAR":
        raise RuntimeError("Cancelado sin alterar servicios")
    FreshActivation(ROOT, PROJECT, Path.home()).activate()
    print("Diagnóstico posterior: bash scripts/verificar.sh")
    print("Recuperar una activación interrumpida: python3 scripts/activacion_systemd.py --recover")


def main():
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--check',action='store_true');g.add_argument('--prepare',action='store_true');g.add_argument('--activate',action='store_true')
    a=p.parse_args()
    try:
        if a.check: return 0 if not check() else 2
        if a.prepare:prepare()
        if a.activate:activate()
        return 0
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError) as e:
        print('ERROR:',e,file=sys.stderr)
        return 2
if __name__=='__main__':sys.exit(main())
