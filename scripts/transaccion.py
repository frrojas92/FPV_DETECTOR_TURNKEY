#!/usr/bin/env python3
"""Registro y recuperación de archivos para Turnkey V1.0.

No gestiona systemd ni privilegios sudo.
Solo manipula rutas explícitamente entregadas.
"""

import json
import hashlib
import os
import shutil
from pathlib import Path


def validate_path_no_symlinks(path):
    """Reject symlinks in the destination and its existing parents."""
    path = Path(path)

    if not path.is_absolute():
        raise RuntimeError("Ruta no absoluta")

    for component in (path, *path.parents):
        if component.is_symlink():
            raise RuntimeError(
                f"Enlace simbólico en la ruta: {component}"
            )


class FileTransaction:
    def __init__(self, backup_dir):
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=False)
        self.entries = []
        self.journal = self.backup_dir / "journal.json"
        self._save()

    @classmethod
    def load(cls, backup_dir, allowed_destinations):
        """Load a journal using an explicit destination allowlist."""
        backup_dir = Path(backup_dir).resolve(strict=True)
        journal = backup_dir / "journal.json"

        if journal.is_symlink() or not journal.is_file():
            raise RuntimeError("Registro de transacción no válido")

        allowed = {
            str(Path(path).absolute())
            for path in allowed_destinations
        }

        data = json.loads(journal.read_text())

        if not isinstance(data, list):
            raise RuntimeError("Formato de transacción incorrecto")

        seen = set()

        for entry in data:
            if not isinstance(entry, dict):
                raise RuntimeError("Entrada de transacción inválida")

            if set(entry) != {
                "destination", "existed", "backup", "sha256"
            }:
                raise RuntimeError("Campos de transacción inválidos")

            destination = entry["destination"]

            if (
                not isinstance(destination, str)
                or not Path(destination).is_absolute()
                or str(Path(destination)) != destination
                or destination not in allowed
                or destination in seen
            ):
                raise RuntimeError(
                    "Destino no autorizado o duplicado"
                )

            validate_path_no_symlinks(destination)
            seen.add(destination)

            if type(entry["existed"]) is not bool:
                raise RuntimeError("Estado de archivo inválido")

            if entry["existed"]:
                expected_backup = str(
                    backup_dir / f"file-{len(seen)-1:04d}"
                )
                digest = entry["sha256"]

                if (
                    entry["backup"] != expected_backup
                    or not isinstance(digest, str)
                    or len(digest) != 64
                    or any(c not in "0123456789abcdef" for c in digest)
                ):
                    raise RuntimeError("Respaldo inválido")
            else:
                if entry["backup"] is not None or entry["sha256"] is not None:
                    raise RuntimeError("Entrada de archivo nuevo inválida")

        tx = cls.__new__(cls)
        tx.backup_dir = backup_dir
        tx.journal = journal
        tx.entries = data
        return tx

    def _save(self):
        temp = self.journal.with_suffix(".tmp")
        temp.write_text(
            json.dumps(self.entries, indent=2) + "\n"
        )
        os.replace(temp, self.journal)

    def register(self, destination):
        destination = Path(destination).absolute()
        validate_path_no_symlinks(destination)

        if destination.is_symlink():
            raise RuntimeError(
                f"Enlace simbólico no permitido: {destination}"
            )

        if any(
            entry["destination"] == str(destination)
            for entry in self.entries
        ):
            raise RuntimeError(
                f"Destino duplicado: {destination}"
            )

        existed = destination.exists()

        if existed and not destination.is_file():
            raise RuntimeError(
                f"Destino no es archivo regular: {destination}"
            )

        backup = None

        if existed:
            backup = self.backup_dir / f"file-{len(self.entries):04d}"
            shutil.copy2(destination, backup)

        digest = None

        if existed:
            digest = hashlib.sha256(
                backup.read_bytes()
            ).hexdigest()

            original_digest = hashlib.sha256(
                destination.read_bytes()
            ).hexdigest()

            if digest != original_digest:
                raise RuntimeError(
                    f"Respaldo incorrecto: {destination}"
                )

        entry = {
            "destination": str(destination),
            "existed": existed,
            "backup": str(backup) if backup else None,
            "sha256": digest,
        }

        self.entries.append(entry)
        self._save()

    def restore(self):
        import tempfile

        # Preflight: verify every backup before changing any destination.
        for entry in self.entries:
            destination = Path(entry["destination"])
            validate_path_no_symlinks(destination)

            if destination.is_symlink():
                raise RuntimeError(
                    f"Symlink inesperado: {destination}"
                )

            if destination.exists() and not destination.is_file():
                raise RuntimeError(
                    f"Destino no regular: {destination}"
                )

            if entry["existed"]:
                backup = Path(entry["backup"])

                if not backup.is_file() or backup.is_symlink():
                    raise RuntimeError(
                        f"Respaldo ausente o inválido: {backup}"
                    )

                digest = hashlib.sha256(
                    backup.read_bytes()
                ).hexdigest()

                if digest != entry["sha256"]:
                    raise RuntimeError(
                        f"SHA256 del respaldo incorrecto: {backup}"
                    )

        errors = []

        for entry in reversed(self.entries):
            destination = Path(entry["destination"])
            temporary = None

            try:
                if entry["existed"]:
                    backup = Path(entry["backup"])

                    fd, name = tempfile.mkstemp(
                        prefix=".fpv-restore-",
                        dir=str(destination.parent)
                    )
                    temporary = Path(name)

                    with os.fdopen(fd, "wb") as output:
                        with backup.open("rb") as source:
                            shutil.copyfileobj(source, output)

                        output.flush()
                        os.fsync(output.fileno())

                    shutil.copymode(backup, temporary)

                    if hashlib.sha256(
                        temporary.read_bytes()
                    ).hexdigest() != entry["sha256"]:
                        raise RuntimeError(
                            "Error de integridad en archivo temporal"
                        )

                    os.replace(temporary, destination)

                else:
                    destination.unlink(missing_ok=True)

            except Exception as exc:
                errors.append(f"{destination}: {exc}")

            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

        if errors:
            raise RuntimeError(
                "Restauración incompleta:\\n" +
                "\\n".join(errors)
            )


if __name__ == "__main__":
    print("Módulo FileTransaction disponible")
