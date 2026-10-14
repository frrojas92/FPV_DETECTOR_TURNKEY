import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "fpv_installer_test",
    ROOT / "scripts/instalar.py"
)

installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class TestPrepareSafety(unittest.TestCase):

    def test_reject_existing_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "detector_drone"
            project.mkdir()

            sentinel = project / "original.txt"
            sentinel.write_text("NO MODIFICAR")

            with patch.object(installer, "PROJECT", project), \
                 patch.object(installer, "need_user"), \
                 patch.object(installer, "refuse_running_services"), \
                 patch.object(installer, "prebuild_decoder") as build, \
                 patch.object(installer, "call") as commands:

                with self.assertRaisesRegex(
                    RuntimeError, "directorio destino"
                ):
                    installer.prepare()

                build.assert_not_called()
                commands.assert_not_called()

            self.assertEqual(
                sentinel.read_text(), "NO MODIFICAR"
            )

    def test_prebuild_failure_leaves_no_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "detector_drone"

            with patch.object(installer, "PROJECT", project), \
                 patch.object(installer, "need_user"), \
                 patch.object(installer, "refuse_running_services"), \
                 patch.object(installer, "check", return_value=[]), \
                 patch.object(
                     installer,
                     "prebuild_decoder",
                     side_effect=RuntimeError("CMAKE_TEST_FAILURE")
                 ), \
                 patch.object(installer, "call") as commands:

                with self.assertRaisesRegex(
                    RuntimeError, "CMAKE_TEST_FAILURE"
                ):
                    installer.prepare()

                commands.assert_not_called()

            self.assertFalse(project.exists())


    def test_reject_existing_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "detector_drone"
            project.mkdir()

            with (
                patch.object(installer, "PROJECT", project),
                patch.object(installer, "need_user"),
                patch.object(installer, "refuse_running_services"),
                patch.object(installer, "prebuild_decoder") as compiler,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "directorio destino"
                ):
                    installer.prepare()

                compiler.assert_not_called()

            self.assertTrue(project.is_dir())
            self.assertEqual(list(project.iterdir()), [])

    def test_venv_failure_quarantines_new_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "detector_drone"

            with (
                patch.object(installer, "PROJECT", project),
                patch.object(installer, "need_user"),
                patch.object(installer, "refuse_running_services"),
                patch.object(installer, "check", return_value=[]),
                patch.object(
                    installer,
                    "prebuild_decoder",
                    return_value=root / "fpvdec-test"
                ),
                patch.object(
                    installer,
                    "ensure_venv",
                    side_effect=RuntimeError("PIP_FAILURE_SIMULATED")
                ),
                patch.object(installer, "build_decoder") as publisher,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "PIP_FAILURE_SIMULATED"
                ):
                    installer.prepare()

                publisher.assert_not_called()

            self.assertFalse(project.exists())

            quarantines = list(
                root.glob(".detector_drone.turnkey-incomplete-*")
            )

            self.assertEqual(len(quarantines), 1)
            self.assertTrue(
                (quarantines[0] / "backups_turnkey").is_dir()
            )


if __name__ == "__main__":
    unittest.main()
