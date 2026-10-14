import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "fpv_installer_publication_test",
    ROOT / "scripts/instalar.py"
)

installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class TestFPVDecoderPublication(unittest.TestCase):

    def test_install_precompiled_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "detector_drone"
            generated = root / "fpvdec-prebuilt"

            generated.write_bytes(b"FPS30_PRECOMPILADO_TEST")
            generated.chmod(0o755)

            with (
                patch.object(installer, "PROJECT", project),
                patch.object(installer, "call") as commands,
            ):
                installer.build_decoder(generated)
                commands.assert_not_called()

            binary = (
                project
                / "v19e_fps30_experimental"
                / "build-fps30"
                / "fpvdec"
            )

            self.assertTrue(binary.is_file())
            self.assertEqual(
                binary.read_bytes(),
                generated.read_bytes()
            )
            self.assertTrue(binary.stat().st_mode & 0o111)

    def test_replace_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "detector_drone"

            build = (
                project
                / "v19e_fps30_experimental"
                / "build-fps30"
            )
            build.mkdir(parents=True)

            binary = build / "fpvdec"
            binary.write_bytes(b"BINARIO_ORIGINAL")

            generated = root / "fpvdec-prebuilt"
            generated.write_bytes(b"BINARIO_NUEVO")
            generated.chmod(0o755)

            with (
                patch.object(installer, "PROJECT", project),
                patch(
                    "os.replace",
                    side_effect=OSError("FALLO_SIMULADO")
                ),
                patch.object(installer, "call") as commands,
            ):
                with self.assertRaisesRegex(
                    OSError, "FALLO_SIMULADO"
                ):
                    installer.build_decoder(generated)

                commands.assert_not_called()

            self.assertEqual(
                binary.read_bytes(),
                b"BINARIO_ORIGINAL"
            )

            backups = list(
                (project / "backups_turnkey/fpvdec")
                .glob("fpvdec-*")
            )

            self.assertEqual(len(backups), 1)
            self.assertEqual(
                backups[0].read_bytes(),
                b"BINARIO_ORIGINAL"
            )

            self.assertFalse(
                list(build.glob(".fpvdec-new-*"))
            )


if __name__ == "__main__":
    unittest.main()
