"""Exercise the developer CLI and thin package preparation without changing app configuration."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from voice_rpg.contracts import SCHEMAS, VERSION
from voice_rpg.guide import load_guide

PROJECT = Path(__file__).resolve().parents[1]


def creation():
    return {"campaign": "voyage", "request_id": "create-voyage", "title": "Voyage — 海",
            "preferences": {"tone": "hopeful"}, "character": {
                "name": "Mara", "concept": "A wandering builder", "motivation": "Find the ferryman",
                "talents": ["notice", "craft", "talk"]},
            "opening": {"situation": "The ferry is empty."}}


class CLITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "runs"

    def call(self, command, payload=None, raw=None, extra=()):
        return subprocess.run(
            [sys.executable, str(PROJECT / "rpg"), command, "--data-dir", str(self.data)] + list(extra),
            input=raw if raw is not None else json.dumps(payload or {}),
            text=True, capture_output=True, timeout=20, cwd=self.tmp.name)

    def result(self, process):
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        envelope = json.loads(process.stdout)
        self.assertTrue(envelope["ok"], envelope)
        return envelope["result"]

    def test_generated_contract_and_state_across_processes(self):
        self.assertEqual(set(self.result(self.call("schema"))["commands"]), set(SCHEMAS))
        schema = self.result(self.call("schema", extra=("create",)))
        self.assertEqual(schema["input_schema"], SCHEMAS["create"])
        self.result(self.call("create", creation()))
        resumed = self.result(self.call("resume", {"campaign": "voyage"}))
        self.assertEqual(resumed["campaign"]["title"], "Voyage — 海")
        self.assertEqual(self.result(self.call("resume"))["campaign"]["id"], "voyage")

    def test_invalid_json_never_creates_state(self):
        for raw in ['{"campaign":"one","campaign":"two"}',
                    '{"value":NaN}', '[]', '{bad json}', '{"campaign":"../escape"}']:
            response = self.call("create", raw=raw)
            self.assertNotEqual(response.returncode, 0, raw)
            self.assertFalse(json.loads(response.stdout)["ok"])
            self.assertNotIn("Traceback", response.stderr)
        self.assertFalse(self.data.exists())

    def test_file_input_and_errors(self):
        source = Path(self.tmp.name) / "request.json"
        source.write_text(json.dumps(creation()), encoding="utf-8")
        self.result(self.call("create", extra=("--input", str(source))))
        bad = self.call("resume", {"campaign": "missing"})
        self.assertNotEqual(bad.returncode, 0)
        self.assertFalse(json.loads(bad.stdout)["ok"])
        self.assertNotEqual(self.call("schema", extra=("not-a-command",)).returncode, 0)
        self.assertNotEqual(self.call("restore").returncode, 0)

    def test_guide_body_is_canonical_and_doctor_is_read_only(self):
        output = self.call("guide")
        self.assertEqual(output.returncode, 0)
        self.assertEqual(output.stdout, load_guide())
        self.assertNotIn("name: voice-rpg", output.stdout)
        report = self.result(self.call("doctor"))
        self.assertEqual(report["version"], VERSION)
        self.assertEqual(report["database"], str(self.data.resolve() / "game.sqlite"))
        self.assertEqual(report["project"], str(PROJECT))
        self.assertFalse(report["database_exists"])
        self.assertFalse(self.data.exists())

    def test_pending_check_survives_backup_restore_and_exact_retry(self):
        self.result(self.call("create", creation()))
        begin_args = {"campaign": "voyage", "expected_revision": 0, "request_id": "cross",
                      "intent": "Cross the unstable ferry", "stakes": "Risk a fall", "talent": "craft"}
        begun = self.result(self.call("begin", begin_args))
        backup = self.result(self.call("backup"))["backup"]
        self.assertTrue(Path(backup).is_file())
        self.result(self.call("cancel", {"campaign": "voyage", "action_id": begun["action_id"], "reason": "Wait"}))
        self.result(self.call("restore", extra=(backup,)))
        retry = self.result(self.call("begin", begin_args))
        self.assertTrue(retry["replayed"])
        self.assertEqual(retry["action_id"], begun["action_id"])
        self.assertEqual(retry["check"]["dice"], begun["check"]["dice"])
        self.assertTrue(self.result(self.call("doctor"))["database_compatible"])

    def test_maintenance_restore_can_replace_a_corrupt_live_database(self):
        self.result(self.call("create", creation()))
        backup = self.result(self.call("backup"))["backup"]
        (self.data / "game.sqlite").write_bytes(b"damaged database")
        result = self.result(self.call("restore", extra=(backup,)))
        self.assertTrue(result["refresh_required"])
        self.assertEqual(self.result(self.call("resume"))["campaign"]["revision"], 0)

    def test_installer_package_is_thin_absolute_and_preserves_unrelated_skills(self):
        spec = importlib.util.spec_from_file_location("rpg_installer", PROJECT / "install.py")
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        checkout = Path(self.tmp.name) / "checkout with spaces"
        checkout.mkdir()
        marketplace = installer.prepare_plugin(checkout)
        plugin = marketplace / "plugins" / "voice-rpg"
        config = json.loads((plugin / ".mcp.json").read_text())
        server = config["mcpServers"]["voice-rpg"]
        self.assertEqual(server["command"], str(checkout.resolve() / "rpg-mcp"))
        self.assertEqual(server["args"], ["--data-dir", str(checkout.resolve() / "runs")])
        self.assertEqual(set(path.name for path in plugin.iterdir()), {".codex-plugin", ".mcp.json"})
        manifest = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["mcpServers"], "./.mcp.json")
        catalog = json.loads((marketplace / ".agents" / "plugins" / "marketplace.json").read_text())
        self.assertEqual(catalog["plugins"][0]["source"]["path"], "./plugins/voice-rpg")
        source = checkout / "skills" / "voice-rpg"
        source.mkdir(parents=True)
        home = Path(self.tmp.name) / "home"
        codex_home = home / "custom-codex"
        first = home / ".agents" / "skills" / "voice-rpg"
        second = codex_home / "skills" / "voice-rpg"
        first.parent.mkdir(parents=True)
        second.mkdir(parents=True)
        first.symlink_to(source, target_is_directory=True)
        marker = second / "existing.txt"
        marker.write_text("preserve")
        self.assertEqual(installer.retire_legacy_links(checkout, home, codex_home), [str(first)])
        self.assertFalse(first.exists())
        self.assertEqual(marker.read_text(), "preserve")
        first.symlink_to(second, target_is_directory=True)
        self.assertEqual(installer.retire_legacy_links(checkout, home, codex_home), [])
        self.assertTrue(first.is_symlink())


if __name__ == "__main__":
    unittest.main()
