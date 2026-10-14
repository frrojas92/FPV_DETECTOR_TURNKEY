"""Tests for RC2 automatic touchscreen calibration (no SPI/root/systemd)."""
import ast
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / 'scripts/instalar.py'
WIZARD_PATH = ROOT / 'scripts/calibracion_touch_turnkey.py'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load('rc2_installer_check', INSTALLER_PATH)
wizard = load('rc2_touch_wizard_check', WIZARD_PATH)


class RC2AutoTouchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fpv-autocalib-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'detector_drone'
        self.project.mkdir()
        py = self.project / '.venv-tft/bin/python'
        py.parent.mkdir(parents=True)
        py.write_text('#!/bin/sh\n')
        py.chmod(0o755)
        (self.project / 'display_tft_v19e_fps30.py').write_text('# dummy\n')
        self.cal = self.project / 'fpv_touch_calibration.json'

    def write_valid_cal(self):
        self.cal.write_text(json.dumps({'schema': 1, 'xcoef': [1.0, 0.0, 0.0], 'ycoef': [0.0, 1.0, 0.0]}))

    def test_prepare_calls_touch_at_end(self):
        tree = ast.parse(INSTALLER_PATH.read_text())
        f = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'prepare')
        self.assertIsInstance(f.body[-1], ast.Expr)
        self.assertEqual(f.body[-1].value.func.id, 'post_prepare_touch')

    def test_activate_requires_calibration(self):
        source = INSTALLER_PATH.read_text()
        tree = ast.parse(source)
        f = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'activate')
        body = ast.get_source_segment(source, f)
        self.assertIn('check_calibration(PROJECT / "fpv_touch_calibration.json")', body)
        self.assertNotIn('offer_calibration(PROJECT)', body)

    def test_post_prepare_starts_auto_wizard_only(self):
        installer.ROOT = ROOT
        with patch.object(installer, 'PROJECT', self.project), patch.object(installer.subprocess, 'run') as run:
            installer.post_prepare_touch()
            args, kwargs = run.call_args
            self.assertIn('--auto', args[0])
            self.assertIn(str(self.project), args[0])
            self.assertTrue(kwargs['check'])

    def test_valid_calibration_passes_wizard_checker(self):
        self.write_valid_cal()
        self.assertTrue(wizard.check_calibration(self.cal))

    def test_invalid_calibration_is_not_valid(self):
        self.cal.write_text('not a JSON')
        self.assertFalse(wizard.check_calibration(self.cal))

    def test_cli_supports_auto(self):
        source = WIZARD_PATH.read_text()
        self.assertIn('ap.add_argument("--auto"', source)
        self.assertIn('if args.auto:', source)

    def test_cli_auto_calibrates_when_missing(self):
        from types import SimpleNamespace
        def do_calibrate(project):
            self.assertEqual(project, self.project)
            self.write_valid_cal()
        with (
            patch.object(wizard.sys, 'argv', ['wizard', '--project', str(self.project), '--auto']),
            patch.object(wizard.subprocess, 'run', return_value=SimpleNamespace(returncode=3)),
            patch.object(wizard.sys.stdin, 'isatty', return_value=True),
            patch.object(wizard, 'calibrate', side_effect=do_calibrate) as captured,
        ):
            self.assertEqual(wizard.cli(), 0)
            captured.assert_called_once_with(self.project)
        self.assertTrue(wizard.check_calibration(self.cal))

    def test_cli_auto_skips_existing(self):
        from types import SimpleNamespace
        self.write_valid_cal()
        with (
            patch.object(wizard.sys, 'argv', ['wizard', '--project', str(self.project), '--auto']),
            patch.object(wizard.subprocess, 'run', return_value=SimpleNamespace(returncode=3)),
            patch.object(wizard, 'calibrate') as captured,
        ):
            self.assertEqual(wizard.cli(), 0)
            captured.assert_not_called()

    def test_venv_symlinks_not_rejected_unconditionally(self):
        source = INSTALLER_PATH.read_text()
        self.assertNotIn('if venv.is_symlink() or not venv.is_file():', source)
        self.assertTrue('sys.prefix' in source or 'venv_dir' in source)


if __name__ == '__main__':
    unittest.main()
