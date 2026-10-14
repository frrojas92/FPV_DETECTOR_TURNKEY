#!/usr/bin/env python3
"""Interactive XPT2046 TFT setup for *prepared* Turnkey installations.

Never owns a private calibration schema: invokes the calibration offered by
installed display_tft_v19e_fps30.py and validates its persisted result.
No sudo/systemd mutations. The caller ensures no live display owns SPI.
"""
import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


class CalibrationError(RuntimeError):
    pass


def check_calibration(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return False
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("schema") != 1:
            return False
        for name in ("xcoef", "ycoef"):
            a = d.get(name)
            if not isinstance(a, list) or len(a) != 3 or not all(
                isinstance(v, (float, int)) and not isinstance(v, bool)
                and math.isfinite(v) for v in a
            ):
                return False
    except (OSError, json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return False
    return True


def _files(project):
    project = Path(project)
    if project.is_symlink() or not project.is_dir():
        raise CalibrationError("Destino Turnkey ausente o symlink")
    venv = project / ".venv-tft"
    py = venv / "bin/python"
    if venv.is_symlink() or py.is_symlink() and not py.resolve().is_file():
        raise CalibrationError("Intérprete TFT inválido")
    if not py.is_file() or not os.access(py, os.X_OK):
        raise CalibrationError("Falta Python TFT; ejecutar --prepare")
    tft = project / "display_tft_v19e_fps30.py"
    if tft.is_symlink() or not tft.is_file():
        raise CalibrationError("Falta display_tft_v19e_fps30.py")
    cal = project / "fpv_touch_calibration.json"
    if cal.is_symlink():
        raise CalibrationError("Calibración es un enlace simbólico; revisar manualmente")
    return py, tft, cal


def calibrate(project, *, manual=False, runner=subprocess.run):
    py, tft, cal = _files(project)
    if not Path("/dev/spidev0.1").exists():
        raise CalibrationError("Falta /dev/spidev0.1; activar dtparam=spi=on y reiniciar")
    help_result = runner([str(py), str(tft), "--help"], capture_output=True,
                         text=True, check=True, timeout=25, cwd=str(project))
    help_text = help_result.stdout + help_result.stderr
    if "--touch-calibrate" not in help_text or "--calibration-file" not in help_text:
        raise CalibrationError(
            "El TFT instalado no ofrece --touch-calibrate/--calibration-file; "
            "no se fabricará un JSON incompatible"
        )
    if manual and "--touch-calibrate-manual" not in help_text:
        raise CalibrationError("El TFT no ofrece --touch-calibrate-manual")
    backup = None
    if cal.exists():
        backup = cal.with_name(cal.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        shutil.copy2(cal, backup)
        print(f"Respaldo previo: {backup}")
    mode = "--touch-calibrate-manual" if manual else "--touch-calibrate"
    args = [str(py), "-u", str(tft), mode, "--calibration-file", str(cal)]
    try:
        print("Toca las cuatro cruces que aparecerán en la TFT de 240x320.", flush=True)
        print("No iniciar simultáneamente el detector ni otro proceso TFT.", flush=True)
        runner(args, cwd=str(project), check=True)
        if not check_calibration(cal):
            raise CalibrationError("El calibrador no produjo un archivo JSON compatible")
    except BaseException:
        # Restore earlier validated data after a failed attempt; never discard it.
        if backup is not None and backup.is_file():
            shutil.copy2(backup, cal)
        raise
    print(f"TOUCH CALIBRADO: {cal}")
    return cal


def offer_calibration(project, *, input_func=input, calibrator=calibrate):
    _, _, cal = _files(project)
    valid = check_calibration(cal)
    question = (
        "Calibración TOUCH existente válida. ¿Recalibrar ahora? [s/N]: "
        if valid else
        "TOUCH sin calibrar. ¿Calibrar ahora en pantalla? [S/n]: "
    )
    answer = input_func(question).strip().lower()
    if answer not in ("s", "si", "sí", "n", "no", ""):
        raise CalibrationError("Respuesta inválida; no se activa")
    requested = answer in ("s", "si", "sí") or (not valid and answer == "")
    if requested:
        calibrator(project)
        if not check_calibration(cal):
            raise CalibrationError("No se encontró una calibración válida")
    elif not valid:
        # Explicit opt-out is possible, but never silent.
        confirm = input_func("Sin calibración, el touch no estará garantizado. "
                             "Escribe OMITIR para continuar: ").strip()
        if confirm != "OMITIR":
            raise CalibrationError("Calibración pendiente; activación cancelada")
    return check_calibration(cal)


def cli():
    ap = argparse.ArgumentParser(description="Calibrar XPT2046 con el TFT FPS30 instalado")
    ap.add_argument("--project", type=Path, default=Path.home() / "detector_drone")
    ap.add_argument("--check", action="store_true", help="Validar solo archivo de calibración")
    ap.add_argument("--manual", action="store_true", help="Calibrar con ENTER en terminal, si IRQ no responde")
    ap.add_argument("--auto", action="store_true", help="Calibrar sin confirmación al terminar --prepare")
    args = ap.parse_args()
    if args.check:
        _, _, cal = _files(args.project)
        ok = check_calibration(cal)
        print("CALIBRADO" if ok else "PENDIENTE", cal)
        return 0 if ok else 1
    # Hardware operations are not allowed with an active display in service.
    for service in ("detector-fpv", "fpv-web-tft"):
        state = subprocess.run(["systemctl", "is-active", "--quiet", service], check=False)
        if state.returncode == 0:
            raise CalibrationError(f"Detener {service} antes de calibrar")
    if args.auto:
        _, _, cal = _files(args.project)
        if check_calibration(cal):
            print("TOUCH: calibración válida existente, se conserva", cal)
            return 0
        if not sys.stdin.isatty():
            raise CalibrationError("Calibración requiere una terminal interactiva (TTY)")
        calibrate(args.project)
    elif args.manual:
        calibrate(args.project, manual=True)
    else:
        offer_calibration(args.project)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(cli())
    except (CalibrationError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print("TOUCH: ", exc, file=sys.stderr)
        sys.exit(1)
