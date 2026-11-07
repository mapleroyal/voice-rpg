"""Exercise Muse registration and the real CLI without modifying an actual skill store."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("muse_installer", PROJECT / "install_muse.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class MuseInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="voice-rpg-muse-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "checkout with 'quotes' and spaces"
        self.project.mkdir()
        for name in ("voice_rpg", "skills"):
            shutil.copytree(PROJECT / name, self.project / name, ignore=shutil.ignore_patterns("__pycache__"))
        self.skills = self.root / "custom skills"
        self.data = self.root / "persistent data"

    def install(self, **kwargs):
        return installer.install(self.project, kwargs.get("skills_dir", self.skills), kwargs.get("data_dir", self.data))

    def call(self, command, payload=None, extra=()):
        launcher = self.skills / installer.SKILL_NAME / "rpg"
        process = subprocess.run([str(launcher), command, *extra], input=json.dumps(payload or {}),
                                 capture_output=True, text=True, cwd=self.root, timeout=10)
        return process, json.loads(process.stdout)

    def create(self):
        return self.call("create", {"campaign": "test", "request_id": "create-test", "title": "Test", "preferences": {},
            "character": {"name": "Ada", "concept": "A courier", "motivation": "Reach the gate",
                          "talents": ["notice", "craft", "talk"]},
            "opening": {"situation": "The gates are closing."}})

    def test_launcher_runs_real_cli_from_other_directory_and_survives_reinstall(self):
        result = self.install()
        self.assertFalse(self.data.exists())
        self.assertEqual(result["data_dir"], str(self.data))
        process, report = self.call("doctor")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertTrue(report["ok"])
        self.assertEqual(report["result"]["data_dir"], str(self.data))
        self.assertEqual(report["result"]["project"], str(self.project))
        process, made = self.create()
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertTrue(made["ok"])
        saved = (self.data / "game.sqlite").read_bytes()
        note = self.skills / installer.SKILL_NAME / "personal-note.txt"
        note.write_text("preserve this")
        # Omitting --data-dir during an upgrade preserves a previously selected collection.
        installed = installer.install(self.project, self.skills)
        self.assertEqual(installed["data_dir"], str(self.data))
        self.assertEqual((self.data / "game.sqlite").read_bytes(), saved)
        self.assertEqual(note.read_text(), "preserve this")
        process, resumed = self.call("resume")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(resumed["result"]["campaign"]["id"], "test")
        self.assertEqual(resumed["result"]["campaign"]["revision"], 0)
        self.assertIn("guide", resumed["result"])

    def test_override_and_input_file_are_forwarded_without_changing_default_collection(self):
        self.install()
        isolated = self.root / "temporary test data"
        process, report = self.call("doctor", extra=("--data-dir", str(isolated)))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(report["result"]["data_dir"], str(isolated))
        self.assertFalse(self.data.exists())
        request = self.root / "request with spaces.json"
        request.write_text('{"campaign":"missing"}')
        process, response = self.call("resume", extra=("--input", str(request), "--data-dir", str(isolated)))
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(response["ok"])
        self.assertFalse(self.data.exists())

    def test_uses_current_python_without_checkout_virtualenv(self):
        self.install()
        # A broken optional MCP environment must not affect the stdlib Muse launcher.
        stale = self.project / ".venv" / "bin"
        stale.mkdir(parents=True)
        stale_python = stale / "python"
        stale_python.write_text("#!/bin/sh\nexit 99\n")
        stale_python.chmod(0o755)
        process, report = self.call("doctor")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(Path(report["result"]["interpreter"]).resolve(), Path(shutil.which("python3")).resolve())

    def test_preserves_unrelated_skill_and_refuses_conflicting_files_before_any_write(self):
        destination = self.skills / installer.SKILL_NAME
        destination.mkdir(parents=True)
        existing = destination / "SKILL.md"
        existing.write_text("An unrelated skill")
        with self.assertRaisesRegex(ValueError, "unrelated"):
            self.install()
        self.assertEqual(existing.read_text(), "An unrelated skill")
        self.assertEqual(set(p.name for p in destination.iterdir()), {"SKILL.md"})
        existing.unlink()
        note = destination / "other.txt"
        note.write_text("not ours")
        with self.assertRaisesRegex(ValueError, "unrelated"):
            self.install()
        self.assertEqual(note.read_text(), "not ours")

    def test_refuses_symlinked_skill_or_managed_file(self):
        target = self.root / "other"
        target.mkdir()
        self.skills.mkdir()
        destination = self.skills / installer.SKILL_NAME
        destination.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "non-directory"):
            self.install()
        self.assertEqual(list(target.iterdir()), [])
        destination.unlink()
        self.install()
        launcher = destination / "rpg"
        launcher.unlink()
        untouched = target / "file"
        untouched.write_text("preserve")
        launcher.symlink_to(untouched)
        with self.assertRaisesRegex(ValueError, "unrelated"):
            self.install()
        self.assertEqual(untouched.read_text(), "preserve")

    def test_registration_contains_only_current_checkout_entrypoint_and_metadata(self):
        self.install()
        destination = self.skills / installer.SKILL_NAME
        metadata = dict((key, json.loads(value)) for key, value in
                        (line.split(": ", 1) for line in (destination / "skill.yaml").read_text().splitlines()
                         if not line.startswith("#")))
        self.assertEqual(metadata["name"], destination.name)
        self.assertEqual(metadata["category"], "Media")
        self.assertIs(metadata["include_in_prompt"], True)
        self.assertEqual(set(p.name for p in destination.iterdir()), {"skill.yaml", "SKILL.md", "rpg"})
        self.assertNotIn(str(self.data), (destination / "SKILL.md").read_text())
        self.assertNotIn(str(self.data), (destination / "skill.yaml").read_text())
        self.assertFalse((self.project / "runs").exists())
        process, output = self.call("schema", extra=("create",))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(output["result"]["command"], "create")


if __name__ == "__main__":
    unittest.main()
