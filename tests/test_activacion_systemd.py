import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("activacion_systemd", Path(__file__).resolve().parents[1] / "scripts/activacion_systemd.py")
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


class FakeRunner:
    def __init__(self, fail=None):
        self.commands = []
        self.fail = fail
        self.states = {}

    def __call__(self, args, check=True):
        self.commands.append(args)
        if self.fail and self.fail(args):
            raise RuntimeError("SIMULATED FAILURE")
        if args[:2] == ["sudo", "install"]:
            source, target = map(Path, args[-2:])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            target.chmod(int(args[3], 8))
        elif args[:3] == ["sudo", "rm", "-f"]:
            Path(args[-1]).unlink(missing_ok=True)
        elif args[:3] == ["sudo", "systemctl", "start"]:
            self.states[args[-1]] = "active"
        elif args[:3] == ["sudo", "systemctl", "stop"]:
            self.states[args[-1]] = "inactive"
        elif args[:3] == ["systemctl", "is-active", "--quiet"]:
            if self.states.get(args[-1]) != "active":
                raise RuntimeError("NOT ACTIVE")


class ActivationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fpv-activate46-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.project = self.root / "project"
        self.home = self.root / "home"
        self.system_root = self.root / "system"
        self.repo.mkdir()
        self.project.mkdir()
        self.home.mkdir()
        (self.system_root / "etc/systemd/system").mkdir(parents=True)
        (self.system_root / "etc/profile.d").mkdir(parents=True)
        (self.system_root / "usr/local/bin").mkdir(parents=True)
        (self.project / "backups_turnkey").mkdir()
        for service in MOD.SERVICES:
            src = self.repo / "systemd" / (service + ".service")
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(f"[Unit]\nDescription={service}\n[Service]\nExecStart=/bin/true\n[Install]\nWantedBy=multi-user.target\n")
        for src in list(MOD.HELPERS.values()) + ["src/ssh/90-fpv-ssh-ui.sh"]:
            file = self.repo / src
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("#!/bin/sh\necho hello\n")
        self.runner = FakeRunner()

    def tearDown(self):
        self.temp.cleanup()

    def manager(self):
        return MOD.FreshActivation(
            self.repo, self.project, self.home,
            system_root=self.system_root, run=self.runner)

    def test_clean_activation(self):
        mgr = self.manager()
        mgr.activate()
        self.assertFalse(mgr.journal.exists())
        for key, (_, dest, _) in mgr._items().items():
            self.assertTrue(dest.is_file(), key)
        self.assertEqual(self.runner.states["detector-fpv.service"], "active")

    def test_reject_existing_before_commands(self):
        mgr = self.manager()
        target = mgr.systemd / "detector-fpv.service"
        target.write_text("EXISTING")
        with self.assertRaisesRegex(RuntimeError, "Instalación existente"):
            mgr.activate()
        self.assertEqual(target.read_text(), "EXISTING")
        self.assertEqual(self.runner.commands, [])
        self.assertFalse(mgr.journal.exists())

    def test_fail_installer_rolls_back(self):
        self.runner.fail = lambda args: args[:2] == ["sudo", "install"] and args[-1].endswith("fpv-tak-sensor.service")
        mgr = self.manager()
        with self.assertRaisesRegex(RuntimeError, "SIMULATED FAILURE"):
            mgr.activate()
        self.assertFalse(mgr.journal.exists())
        for _, dest, _ in mgr._items().values():
            self.assertFalse(dest.exists(), dest)

    def test_fail_start_rolls_back(self):
        self.runner.fail = lambda args: args[:3] == ["sudo", "systemctl", "start"] and args[-1] == "detector-fpv.service"
        mgr = self.manager()
        with self.assertRaisesRegex(RuntimeError, "SIMULATED FAILURE"):
            mgr.activate()
        self.assertFalse(mgr.journal.exists())
        for _, dest, _ in mgr._items().values():
            self.assertFalse(dest.exists(), dest)
        self.assertTrue(any(cmd[:3] == ["sudo", "systemctl", "disable"] for cmd in self.runner.commands))

    def test_journal_recover_after_interruption(self):
        mgr = self.manager()
        contents = mgr._contents()
        manifest = mgr._record(contents)
        one_key = "unit:detector-fpv"
        _, dest, _ = mgr._items()[one_key]
        dest.write_bytes(contents[one_key])
        del mgr
        fresh = self.manager()
        fresh.recover()
        self.assertFalse(dest.exists())
        self.assertFalse(fresh.journal.exists())

    def test_refuse_changed_file_during_recovery(self):
        mgr = self.manager()
        contents = mgr._contents()
        mgr._record(contents)
        _, dest, _ = mgr._items()["unit:detector-fpv"]
        dest.write_text("UNRELATED CONTENT")
        with self.assertRaisesRegex(RuntimeError, "cambió"):
            mgr.recover()
        self.assertEqual(dest.read_text(), "UNRELATED CONTENT")
        self.assertTrue(mgr.journal.exists())
        self.assertEqual(self.runner.commands, [])

    def test_refuse_modified_journal_keys(self):
        mgr = self.manager()
        content = mgr._contents()
        mgr._record(content)
        import json
        doc = json.loads(mgr.journal.read_text())
        doc["sha256"]["unit:evil"] = "0" * 64
        mgr.journal.write_text(json.dumps(doc))
        with self.assertRaisesRegex(RuntimeError, "inválido"):
            mgr.recover()
        self.assertEqual(self.runner.commands, [])

    def test_static_unit_not_enabled(self):
        src = self.repo / "systemd" / "fpv-boot-splash.service"
        src.write_text("[Unit]\nDescription=One shot\n[Service]\nExecStart=/bin/true\n")
        mgr = self.manager()
        mgr.activate()
        enabled = [c[-1] for c in self.runner.commands if c[:3] == ["sudo", "systemctl", "enable"]]
        self.assertNotIn("fpv-boot-splash.service", enabled)


if __name__ == "__main__":
    unittest.main()
