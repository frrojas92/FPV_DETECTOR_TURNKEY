import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "calibracion_touch_turnkey", Path(__file__).resolve().parents[1]
    / "scripts/calibracion_touch_turnkey.py"
)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class TouchCalibrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        (self.project / ".venv-tft/bin").mkdir(parents=True)
        py = self.project / ".venv-tft/bin/python"
        py.write_text("#! /usr/bin/env python3\n")
        py.chmod(0o755)
        (self.project / "display_tft_v19e_fps30.py").write_text("# mock\n")
        self.cal = self.project / "fpv_touch_calibration.json"

    def write_cal(self):
        self.cal.write_text(json.dumps({"schema": 1, "xcoef": [0.1, 0, 2], "ycoef": [0, -0.09, 290]}))

    def test_invalid_baseline_rejected(self):
        self.assertFalse(mod.check_calibration(self.cal))
        self.cal.write_text(json.dumps({"schema": 1, "xcoef": [1, 2, 3], "ycoef": [1, 2]}))
        self.assertFalse(mod.check_calibration(self.cal))

    def test_valid_affine_schema_accepted(self):
        self.write_cal()
        self.assertTrue(mod.check_calibration(self.cal))

    def test_empty_response_on_new_installs_calibrates(self):
        called = []
        def fake(project):
            called.append(project)
            self.write_cal()
        self.assertTrue(mod.offer_calibration(self.project, input_func=lambda _: "", calibrator=fake))
        self.assertEqual(called, [self.project])

    def test_refuse_unconfirmed_skip(self):
        answers = iter(["n", "invalid"])
        with self.assertRaisesRegex(mod.CalibrationError, "pendiente"):
            mod.offer_calibration(self.project, input_func=lambda _: next(answers))

    def test_keep_existing_calibration(self):
        self.write_cal()
        def unexpected(_):
            raise AssertionError("Should not calibrate")
        self.assertTrue(mod.offer_calibration(self.project, input_func=lambda _: "", calibrator=unexpected))

    def test_calibration_failure_preserves_prior_file(self):
        self.write_cal()
        before = self.cal.read_bytes()
        def runner(args, **kwargs):
            if "--help" in args:
                return SimpleNamespace(stdout="--touch-calibrate --calibration-file", stderr="")
            self.cal.write_text("BAD")
            raise RuntimeError("SIMULATED")
        # No real hardware needed; mock only SPI path.exists with a narrow primitive.
        original_exists = Path.exists
        def exists(path):
            if str(path) == "/dev/spidev0.1":
                return True
            return original_exists(path)
        with patch.object(Path, "exists", exists):
            with self.assertRaisesRegex(RuntimeError, "SIMULATED"):
                mod.calibrate(self.project, runner=runner)
        self.assertEqual(self.cal.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
