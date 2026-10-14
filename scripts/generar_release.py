#!/usr/bin/env python3
"""Fase 47: build and verify a source-only FPV Turnkey V1.0 RC1 ZIP.

Uses FILE_MANIFEST.json's existing payload allowlist plus audited additions.
Never accesses ~/detector_drone, invokes sudo, or runs systemctl.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

PACKAGE_ROOT = "FPV_DETECTOR_TURNKEY_V1_0"
ZIP_NAME = "FPV_DETECTOR_TURNKEY_V1_0_RC1.zip"
METADATA = ("FILE_MANIFEST.json", "SHA256SUMS.txt")
ADD = (
    "scripts/activacion_systemd.py",
    "scripts/transaccion.py",
    "scripts/generar_release.py",
    "tests/test_activacion_systemd.py",
    "tests/test_transaccion.py",
    "tests/test_instalador_seguridad.py",
    "tests/test_fpvdec_publicacion.py",
)
PRIVATE_KEY = re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
SENSITIVE_NAMES = {".env", "id_rsa", "id_ed25519", "known_hosts", "authorized_keys"}
SENSITIVE_EXTENSIONS = {".pem", ".p12", ".pfx", ".key", ".sqlite", ".db"}


def die(msg):
    raise RuntimeError(msg)


def safe_rel(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        die(f"Ruta de manifiesto inválida: {name!r}")
    path = PurePosixPath(name)
    if (path.is_absolute() or ".." in path.parts or "." in path.parts
            or name.startswith("./") or str(path) != name):
        die(f"Ruta insegura en manifiesto: {name!r}")
    if name in METADATA:
        die(f"Metadato incluido recursivamente en files: {name}")
    if (set(path.parts) & {".git", "__pycache__", ".venv-tft", "backups_turnkey", "fase46_patch", "releases"}
            or any("backup-" in part or part.endswith("~") for part in path.parts)
            or path.name in SENSITIVE_NAMES or path.suffix.lower() in SENSITIVE_EXTENSIONS
            or name.endswith((".pyc", ".tar", ".tar.gz", ".zip"))):
        die(f"Elemento no apto para distribución: {name}")
    return name


def regular_source(repo, relative):
    safe_rel(relative)
    current = repo
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            die(f"Symlink no permitido en el paquete: {current}")
    if not current.is_file() or not stat.S_ISREG(current.stat().st_mode):
        die(f"Archivo faltante o no regular: {current}")
    return current


def digest(content):
    return hashlib.sha256(content).hexdigest()


def verify_archive(archive, files):
    expected_names = {f"{PACKAGE_ROOT}/{p}" for p in files}
    with zipfile.ZipFile(archive, "r") as zf:
        actual_names = [item.filename for item in zf.infolist()]
        if len(actual_names) != len(expected_names) or set(actual_names) != expected_names:
            die("ZIP contiene nombres repetidos, faltantes o inesperados")
        if zf.testzip() is not None:
            die("ZIP corrupto (testzip)")
        for relative, expected in files.items():
            member = zf.getinfo(f"{PACKAGE_ROOT}/{relative}")
            data = zf.read(member)
            if digest(data) != digest(expected) or len(data) != len(expected):
                die(f"Verificación ZIP fallida: {relative}")
            if not stat.S_ISREG(member.external_attr >> 16):
                die(f"Miembro no regular en ZIP: {relative}")
    return len(expected_names)


def atomic_write(path, content):
    fd, tmp = tempfile.mkstemp(prefix=".fpv-release-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def build(repo, output):
    repo = repo.absolute()
    if repo.is_symlink() or not repo.is_dir() or repo.name != PACKAGE_ROOT:
        die(f"Se esperaba el repositorio {PACKAGE_ROOT}: {repo}")
    if repo == (Path.home() / "detector_drone"):
        die("Nunca utilizar el directorio operativo")
    manifest_path = repo / "FILE_MANIFEST.json"
    sha_path = repo / "SHA256SUMS.txt"
    for p in (manifest_path, sha_path):
        if p.is_symlink() or not p.is_file():
            die(f"Falta metadato original regular: {p}")
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(original, dict) or not isinstance(original.get("files"), list):
        die("FILE_MANIFEST.json no posee files como lista")

    candidates = []
    for record in original["files"]:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            die("Registro inválido en files")
        if record["path"] not in METADATA:
            candidates.append(record["path"])
    candidates.extend(ADD)
    candidates = [safe_rel(p) for p in candidates]
    if len(candidates) != len(set(candidates)):
        # An added file may already be present from a previous packaging run.
        candidates = list(dict.fromkeys(candidates))
    candidates.sort()
    if len(candidates) < 50:
        die(f"Lista sospechosamente corta ({len(candidates)} archivos)")

    payload = {}
    rows = []
    for rel in candidates:
        source = regular_source(repo, rel)
        content = source.read_bytes()
        if PRIVATE_KEY.search(content):
            die(f"Posible clave privada detectada; abortando: {rel}")
        payload[rel] = content
        rows.append({"path": rel, "sha256": digest(content), "size": len(content)})

    manifest = dict(original)
    manifest["release"] = "v1.0.0-rc1"
    manifest["files"] = rows
    # Keep known hardware validation blocker visible to a receiver.
    old_blockers = manifest.get("known_blockers", [])
    if not isinstance(old_blockers, list):
        old_blockers = []
    blocker = "Second Raspberry Pi 5 end-to-end hardware installation test required"
    manifest["known_blockers"] = list(dict.fromkeys([*old_blockers, blocker]))
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    sums_bytes = "".join(f'{row["sha256"]}  {row["path"]}\n' for row in rows).encode()
    payload["FILE_MANIFEST.json"] = manifest_bytes
    payload["SHA256SUMS.txt"] = sums_bytes

    # Require destination outside repository to prevent archive recursion.
    output = output.absolute()
    if output == repo or repo in output.parents:
        die("El ZIP de distribución debe estar FUERA del repositorio")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output.is_symlink():
        die("Directorio de salida no puede ser symlink")
    destination = output / ZIP_NAME
    if destination.exists() or destination.is_symlink():
        die(f"Archivo de salida existente; no se sobrescribirá: {destination}")

    fd, tmp = tempfile.mkstemp(prefix=".fpv-rc1-", suffix=".zip", dir=str(output))
    os.close(fd)
    temporary = Path(tmp)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, allowZip64=True) as zf:
            for relative, content in sorted(payload.items()):
                info = zipfile.ZipInfo(f"{PACKAGE_ROOT}/{relative}", date_time=(2026, 10, 14, 0, 0, 0))
                info.create_system = 3
                executable = bool(regular_source(repo, relative).stat().st_mode & 0o111) if relative not in METADATA else False
                info.external_attr = ((stat.S_IFREG | (0o755 if executable else 0o644)) << 16)
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        count = verify_archive(temporary, payload)
        # Check that source content did not change while archive was constructed.
        for relative in candidates:
            if digest(regular_source(repo, relative).read_bytes()) != digest(payload[relative]):
                die(f"Fuente cambió durante empaquetado: {relative}")

        # Preserve the last-known manifest files OUTSIDE the archived repository.
        backup_dir = output / ("metadata-pre-rc1-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        backup_dir.mkdir(mode=0o700)
        shutil.copy2(manifest_path, backup_dir / manifest_path.name)
        shutil.copy2(sha_path, backup_dir / sha_path.name)
        atomic_write(manifest_path, manifest_bytes)
        atomic_write(sha_path, sums_bytes)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    print("FASE 47: ZIP RC1 GENERADO Y VERIFICADO")
    print("ZIP:", destination)
    print("ARCHIVOS PAYLOAD:", len(rows))
    print("TOTAL EN ZIP:", count)
    print("SHA256 ZIP:", digest(destination.read_bytes()))
    print("METADATOS ANTERIORES:", backup_dir)
    print("PRUEBA DE HARDWARE EN OTRA RPi5: PENDIENTE")
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path.home() / "releases_fpv")
    args = parser.parse_args()
    build(args.repo, args.output)


if __name__ == "__main__":
    main()
