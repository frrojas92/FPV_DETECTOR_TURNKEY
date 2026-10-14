#!/usr/bin/env python3
"""Clean-install-only systemd activation with bounded, recoverable rollback.

Never load filesystem destinations from a journal. All destinations are fixed
by this module, and existing installations are rejected rather than replaced.
"""
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

SERVICES = (
    "fpv-boot-splash", "detector-fpv", "fpv-web-tft",
    "fpv-tak-sensor", "fpv-usb-volume",
)
START = ("fpv-web-tft", "fpv-tak-sensor", "detector-fpv")
STOP = tuple(reversed(START))
HELPERS = {
    "fpv19e-tmux-supervisor": "systemd/fpv19e-tmux-supervisor.sh",
    "fpv-usb-volume": "src/audio/fpv-usb-volume.sh",
    "fpv-banner": "src/ssh/fpv-banner",
    "fpv": "src/ssh/fpv",
}
LOCAL = {
    "fpv-tak": "fpv_tak_tui.py",
    "fpv-tak-softcert": "cargar_softcert.py",
}
JOURNAL = "activation-pending.json"


def _digest(content):
    return hashlib.sha256(content).hexdigest()


def _safe_path(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError(f"Ruta insegura: {path}")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise RuntimeError(f"Enlace simbólico en ruta: {component}")
    return path


def _write_json_atomic(path, obj, *, first=False):
    """Commit a small journal and fsync the containing directory."""
    path = _safe_path(path)
    if first:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(obj, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    else:
        fd, temp = tempfile.mkstemp(prefix=".fpv-act-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(obj, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, path)
        finally:
            Path(temp).unlink(missing_ok=True)
    _fsync_dir(path.parent)


def _fsync_dir(directory):
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _enabled(unit_content):
    section = False
    for raw in unit_content.decode("utf-8").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line == "[Install]"
        elif section and re.match(r"^(WantedBy|RequiredBy|UpheldBy|Alias)\s*=", line):
            return True
    return False


class FreshActivation:
    def __init__(self, repo, project, home, *, system_root=Path("/"), run=None):
        self.repo = Path(repo)
        self.project = _safe_path(Path(project).absolute())
        self.home = _safe_path(Path(home).absolute())
        root = _safe_path(Path(system_root).absolute())
        self.systemd = root / "etc/systemd/system"
        self.system_bin = root / "usr/local/bin"
        self.profile = root / "etc/profile.d/90-fpv-ssh-ui.sh"
        self.local_bin = self.home / ".local/bin"
        self.journal = self.project / "backups_turnkey" / JOURNAL
        self.run = run or (lambda args, check=True: subprocess.run(args, check=check))

    def _items(self):
        """Immutable destination keys; never accept paths from JSON."""
        items = {}
        for name in SERVICES:
            items[f"unit:{name}"] = (
                self.repo / "systemd" / f"{name}.service",
                self.systemd / f"{name}.service", 0o644,
            )
        for name, relative in HELPERS.items():
            items[f"helper:{name}"] = (
                self.repo / relative, self.system_bin / name, 0o755,
            )
        items["profile"] = (
            self.repo / "src/ssh/90-fpv-ssh-ui.sh", self.profile, 0o644,
        )
        for name in LOCAL:
            items[f"local:{name}"] = (None, self.local_bin / name, 0o755)
        return items

    def _contents(self):
        content = {}
        for key, (source, destination, mode) in self._items().items():
            _safe_path(destination)
            if source is not None:
                if source.is_symlink() or not source.is_file():
                    raise RuntimeError(f"Fuente inválida: {source}")
                content[key] = source.read_bytes()
            else:
                script = LOCAL[key.removeprefix("local:")]
                content[key] = (
                    '#!/usr/bin/env bash\n'
                    f'exec /usr/bin/python3 "$HOME/detector_drone/{script}" "$@"\n'
                ).encode()
        return content

    def _preflight(self):
        if self.journal.exists() or self.journal.is_symlink():
            raise RuntimeError(
                f"Hay una activación pendiente: {self.journal}. Recuperar primero."
            )
        for key, (_, dest, _) in self._items().items():
            _safe_path(dest)
            if dest.exists() or dest.is_symlink():
                raise RuntimeError(f"Instalación existente; prohibido sobrescribir: {dest}")
        for name in SERVICES:
            unit_dir = self.systemd / f"{name}.service.d"
            _safe_path(unit_dir)
            if unit_dir.exists() or unit_dir.is_symlink():
                raise RuntimeError(f"Overrides existentes: {unit_dir}")
        # Refuse units defined elsewhere (vendor, run, masked, generated).
        # Supported only with a real root; tests use an isolated fake root.
        if self.systemd == Path("/etc/systemd/system"):
            for name in SERVICES:
                result = subprocess.run(
                    ["systemctl", "show", "-p", "LoadState", "--value", f"{name}.service"],
                    capture_output=True, text=True, check=False,
                )
                if result.returncode or result.stdout.strip() != "not-found":
                    raise RuntimeError(
                        f"Unidad systemd ya existente o estado incierto: {name}: "
                        f"{result.stdout.strip()} {result.stderr.strip()}"
                    )

    def _record(self, contents):
        enable = [name for name in SERVICES if _enabled(contents[f"unit:{name}"])]
        manifest = {
            "version": 1, "state": "pending",
            "project": str(self.project),
            "sha256": {key: _digest(data) for key, data in contents.items()},
            "enable": enable,
        }
        self.journal.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_json_atomic(self.journal, manifest, first=True)
        return manifest

    def _read_record(self):
        _safe_path(self.journal)
        stat_result = self.journal.stat()
        if (not stat.S_ISREG(stat_result.st_mode)
            or stat_result.st_uid != os.getuid()
            or stat.S_IMODE(stat_result.st_mode) != 0o600):
            raise RuntimeError("Diario de activación inseguro")
        data = json.loads(self.journal.read_text())
        keys = set(self._items())
        if (set(data) != {"version", "state", "project", "sha256", "enable"}
            or data["version"] != 1 or data["state"] != "pending"
            or data["project"] != str(self.project)
            or not isinstance(data["sha256"], dict)
            or set(data["sha256"]) != keys
            or any(not isinstance(d, str) or not re.fullmatch(r"[0-9a-f]{64}", d)
                   for d in data["sha256"].values())
            or not isinstance(data["enable"], list)
            or any(name not in SERVICES for name in data["enable"])
            or len(data["enable"]) != len(set(data["enable"]))):
            raise RuntimeError("Diario de activación inválido")
        return data

    def _verify_rollback(self, data):
        for key, (_, destination, _) in self._items().items():
            _safe_path(destination)
            if destination.exists():
                if not destination.is_file():
                    raise RuntimeError(f"Archivo gestionado alterado: {destination}")
                if _digest(destination.read_bytes()) != data["sha256"][key]:
                    raise RuntimeError(
                        f"Archivo gestionado cambió: {destination}. "
                        "No se eliminará nada automáticamente."
                    )

    def _rollback(self, data):
        self._verify_rollback(data)  # All checks before ANY removal.
        errors = []
        for name in STOP:
            try:
                self.run(["sudo", "systemctl", "stop", f"{name}.service"])
            except Exception as exc:
                errors.append(f"stop {name}: {exc}")
        for name in reversed(data["enable"]):
            try:
                self.run(["sudo", "systemctl", "disable", f"{name}.service"])
            except Exception as exc:
                errors.append(f"disable {name}: {exc}")
        # Avoid root operations on user-writable paths.
        for key, (_, destination, _) in reversed(list(self._items().items())):
            if not destination.exists():
                continue
            try:
                if key.startswith("local:"):
                    destination.unlink()
                else:
                    self.run(["sudo", "rm", "-f", "--", str(destination)])
            except Exception as exc:
                errors.append(f"rm {destination}: {exc}")
        try:
            self.run(["sudo", "systemctl", "daemon-reload"])
        except Exception as exc:
            errors.append(f"daemon-reload: {exc}")
        if errors:
            raise RuntimeError("Recuperación incompleta:\n" + "\n".join(errors))
        self.journal.unlink()
        _fsync_dir(self.journal.parent)

    def recover(self):
        data = self._read_record()
        self._rollback(data)
        print("RECUPERACIÓN COMPLETA: archivos Turnkey eliminados; servicios deshabilitados")

    def activate(self):
        self._preflight()
        contents = self._contents()
        self.run(["sudo", "-v"])
        data = self._record(contents)
        try:
            self.local_bin.mkdir(parents=True, exist_ok=True)
            for key, (source, destination, mode) in self._items().items():
                if key.startswith("local:"):
                    # Publish only complete helper scripts; never replace an existing file.
                    fd, tmp = tempfile.mkstemp(prefix=".fpv-helper-", dir=str(destination.parent))
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(contents[key])
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.chmod(tmp, mode)
                        os.link(tmp, destination)  # Atomic, fails if destination exists.
                        _fsync_dir(destination.parent)
                    finally:
                        Path(tmp).unlink(missing_ok=True)
                else:
                    self.run(["sudo", "install", "-m", f"{mode:04o}", str(source), str(destination)])
                if _digest(destination.read_bytes()) != data["sha256"][key]:
                    raise RuntimeError(f"Copia no coincide con manifiesto: {destination}")
            self.run(["sudo", "systemctl", "daemon-reload"])
            for name in data["enable"]:
                self.run(["sudo", "systemctl", "enable", f"{name}.service"])
            for name in START:
                self.run(["sudo", "systemctl", "start", f"{name}.service"])
            for name in START:
                self.run(["systemctl", "is-active", "--quiet", f"{name}.service"])
        except Exception as exc:
            try:
                self._rollback(data)
            except Exception as recovery_exc:
                raise RuntimeError(
                    f"Activación fallida: {exc}. RECUPERACIÓN PENDIENTE: "
                    f"{recovery_exc}. Diario: {self.journal}"
                ) from exc
            raise
        # Committed: no rollback is performed after this boundary.
        data["state"] = "committed"
        _write_json_atomic(self.journal, data)
        self.journal.unlink()
        _fsync_dir(self.journal.parent)
        print("ACTIVACIÓN COMPLETADA: " + ", ".join(START))


def recover_cli(repo, project, home):
    tx = FreshActivation(repo, project, home)
    if not tx.journal.is_file():
        raise RuntimeError(f"No hay diario de recuperación: {tx.journal}")
    confirmation = input("Recuperar activación incompleta. Escribe RECUPERAR: ").strip()
    if confirmation != "RECUPERAR":
        raise RuntimeError("Cancelado")
    tx.run(["sudo", "-v"])
    tx.recover()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Recuperar activación Turnkey interrumpida")
    parser.add_argument("--recover", action="store_true", help="Deshacer activación incompleta")
    args = parser.parse_args()
    if not args.recover:
        parser.error("Solo se permite --recover; --activate se ejecuta mediante instalar.py")
    if os.geteuid() == 0:
        parser.error("Ejecutar como usuario cetel, no como root")
    recover_cli(Path(__file__).resolve().parents[1], Path.home() / "detector_drone", Path.home())
