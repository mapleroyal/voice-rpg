"""Mock-only coverage of tunnel setup; never accesses Keychain or launchd."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_tunnel", PROJECT / "install_tunnel.py")
tunnel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tunnel)

TUNNEL = "tunnel_" + "a" * 32
SECRET = "synthetic-test-key-with-'quotes';$(characters)"


class TunnelSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.project = self.home / "checkout with spaces"
        (self.project / ".venv" / "bin").mkdir(parents=True)
        (self.project / ".venv" / "bin" / "python").touch()
        self.client = self.home / "tunnel-client"
        self.client.touch()
        self.layout = tunnel.paths(self.home)
        self.patch_project = patch.object(tunnel, "PROJECT", self.project)
        self.patch_client = patch.object(tunnel, "CLIENT", self.client)
        self.patch_project.start()
        self.patch_client.start()
        self.addCleanup(self.patch_project.stop)
        self.addCleanup(self.patch_client.stop)
        self.real_preflight = tunnel.preflight_keychain
        self.patch_preflight = patch.object(tunnel, "preflight_keychain")
        self.preflight = self.patch_preflight.start()
        self.addCleanup(self.patch_preflight.stop)

    def options(self, **changes):
        values = dict(tunnel_id=TUNNEL, data_dir=self.project / ".integration" / "acceptance-runs",
                      prepare_only=False, replace_key=False, key_file=False)
        values.update(changes)
        return argparse.Namespace(**values)

    def test_artifacts_contain_references_only_and_one_supervised_foreground_target(self):
        files = tunnel.render(TUNNEL, self.project / "runs", self.layout, self.project)
        profile = json.loads(files[self.layout["profile"]])
        self.assertEqual(profile["control_plane"]["api_key"], "env:CONTROL_PLANE_API_KEY")
        self.assertEqual(profile["health"]["listen_addr"], "127.0.0.1:0")
        self.assertEqual(profile["mcp"]["commands"][0]["channel"], "main")
        self.assertTrue(profile["mcp"]["stdio_send_initialized_notification"])
        self.assertIn(str(self.project / "rpg-mcp"), profile["mcp"]["commands"][0]["command"])
        agent = plistlib.loads(files[self.layout["plist"]])
        self.assertEqual(agent["Label"], tunnel.LABEL)
        self.assertEqual(agent["ProgramArguments"], [str(self.layout["wrapper"])])
        self.assertTrue(agent["KeepAlive"])
        self.assertNotIn("EnvironmentVariables", agent)
        wrapper = files[self.layout["wrapper"]].decode()
        self.assertIn("install_tunnel.py", wrapper)
        self.assertIn("_run", wrapper)
        self.assertNotIn("security", wrapper)  # secret handling has one implementation
        self.assertNotIn(SECRET, repr(files))

    def test_secret_is_only_stdin_to_security_with_narrow_acl_and_verified_readback(self):
        with patch.object(tunnel, "command", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
             patch.object(tunnel, "get_key", return_value=SECRET):
            tunnel.store_key(SECRET)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/bin/security", "-i"])
        self.assertNotIn(SECRET, repr(args))
        self.assertNotIn(SECRET.encode().hex(), repr(args))
        self.assertIn("-T /usr/bin/security", kwargs["input"])
        self.assertNotIn("-A", kwargs["input"])
        self.assertNotIn("-U", kwargs["input"])
        self.assertIn("-X " + SECRET.encode().hex(), kwargs["input"])
        self.assertEqual(kwargs["input"].count("\n"), 1)
        self.assertNotIn("quit", kwargs["input"])
        with patch.object(tunnel, "command", return_value=subprocess.CompletedProcess([], 0, SECRET, SECRET)), \
             patch.object(tunnel, "get_key", return_value="wrong"):
            with self.assertRaises(tunnel.SetupError) as caught:
                tunnel.store_key(SECRET)
            self.assertNotIn(SECRET, str(caught.exception))

    def test_key_update_preserves_existing_access_list(self):
        with patch.object(tunnel, "command", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
             patch.object(tunnel, "get_key", return_value=SECRET):
            tunnel.store_key(SECRET, update=True)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/bin/security", "-i"])
        self.assertIn(" -U ", kwargs["input"])
        self.assertNotIn(" -T ", kwargs["input"])
        self.assertNotIn(" -A", kwargs["input"])
        self.assertNotIn(SECRET, repr(args))

    def test_interactive_line_limit_is_enforced_before_running_security(self):
        overhead = len(tunnel.keychain_request("x")) - 2
        maximum = (tunnel.KEYCHAIN_LINE_LIMIT - 1 - overhead) // 2
        self.assertLess(len(tunnel.keychain_request("x" * maximum)), tunnel.KEYCHAIN_LINE_LIMIT)
        with patch.object(tunnel, "command") as command:
            with self.assertRaises(tunnel.SetupError):
                tunnel.store_key("x" * (maximum + 1))
        command.assert_not_called()

    def test_keychain_interaction_error_is_actionable_and_never_echoes_diagnostics(self):
        result = subprocess.CompletedProcess([], 36, SECRET, "SecKeychainItemCreateFromContent: User interaction is not allowed.\nadd-generic-password: returned -25308\n")
        with patch.object(tunnel, "command", return_value=result):
            with self.assertRaises(tunnel.SetupError) as caught:
                tunnel.store_key(SECRET)
        self.assertIn("/usr/bin/security unlock-keychain", str(caught.exception))
        self.assertNotIn(SECRET, str(caught.exception))

    def test_preflight_status_and_locked_keychain_fail_before_secret_input(self):
        for code in (0, 2, 3):
            with patch.object(tunnel, "command", return_value=subprocess.CompletedProcess([], code, "", "")) as command:
                if code:
                    with self.assertRaises(tunnel.SetupError):
                        self.real_preflight()
                else:
                    self.real_preflight()
                self.assertEqual(command.call_args.args[0], [tunnel.sys.executable, "-c", tunnel.KEYCHAIN_STATUS_SCRIPT])
        self.preflight.side_effect = tunnel.SetupError(tunnel.KEYCHAIN_UNLOCK_MESSAGE)
        with patch.object(tunnel, "job", return_value={"loaded": False, "pid": None}), \
             patch.object(tunnel, "get_key") as key, patch.object(tunnel, "prompt_key") as prompt, \
             patch.object(tunnel, "doctor") as doctor, patch.object(tunnel, "stop") as stop:
            with self.assertRaises(tunnel.SetupError):
                tunnel.setup(self.options(), self.layout)
        for operation in (key, prompt, doctor, stop):
            operation.assert_not_called()

    def test_hidden_prompt_never_falls_back_to_echoing_input(self):
        with patch.object(tunnel.sys.stdin, "isatty", return_value=False), \
             patch.object(tunnel.getpass, "getpass") as prompt:
            with self.assertRaises(tunnel.SetupError):
                tunnel.prompt_key()
            prompt.assert_not_called()

    def test_prepare_only_has_no_key_access_doctor_or_service_start(self):
        with patch.object(tunnel, "job", return_value={"loaded": False, "pid": None}), \
             patch.object(tunnel, "get_key") as key, patch.object(tunnel, "doctor") as doctor, \
             patch.object(tunnel, "start") as start:
            result = tunnel.setup(self.options(prepare_only=True), self.layout)
        self.assertFalse(result["key_accessed"])
        key.assert_not_called()
        doctor.assert_not_called()
        start.assert_not_called()
        self.preflight.assert_not_called()
        self.assertEqual(self.layout["profile"].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.layout["wrapper"].stat().st_mode & 0o777, 0o700)

    def test_failed_doctor_preserves_service_and_never_stores_candidate(self):
        with patch.object(tunnel, "job", return_value={"loaded": True, "pid": 12}), \
             patch.object(tunnel, "get_key", return_value=None), \
             patch.object(tunnel, "prompt_key", return_value=SECRET), \
             patch.object(tunnel, "doctor", side_effect=tunnel.SetupError("failed")), \
             patch.object(tunnel, "store_key") as store, patch.object(tunnel, "stop") as stop:
            with self.assertRaises(tunnel.SetupError):
                tunnel.setup(self.options(), self.layout)
        store.assert_not_called()
        stop.assert_not_called()
        self.assertFalse(self.layout["plist"].exists())

    def test_data_directory_switch_reuses_key_and_tunnel(self):
        original = tunnel.render(TUNNEL, self.project / "acceptance", self.layout, self.project)
        tunnel.write_artifacts(original, self.layout)
        report = {"loaded": True, "pid": 12, "healthy": True, "ready": True}
        with patch.object(tunnel, "job", return_value={"loaded": True, "pid": 12}), \
             patch.object(tunnel, "get_key", return_value=SECRET), \
             patch.object(tunnel, "prompt_key") as prompt, \
             patch.object(tunnel, "store_key") as store, patch.object(tunnel, "doctor"), \
             patch.object(tunnel, "stop"), patch.object(tunnel, "start"), \
             patch.object(tunnel, "status", return_value=report):
            result = tunnel.setup(self.options(tunnel_id=None, data_dir=self.project / "runs"), self.layout)
        prompt.assert_not_called()
        store.assert_not_called()
        self.assertEqual(result, report)
        self.assertEqual(tunnel.previous_settings(self.layout), (TUNNEL, self.project / "runs"))

    def test_start_failure_restores_owned_files_and_previous_service(self):
        original = tunnel.render(TUNNEL, self.project / "acceptance", self.layout, self.project)
        tunnel.write_artifacts(original, self.layout)
        with patch.object(tunnel, "job", return_value={"loaded": True, "pid": 12}), \
             patch.object(tunnel, "get_key", return_value=SECRET), \
             patch.object(tunnel, "doctor"), patch.object(tunnel, "stop") as stop, \
             patch.object(tunnel, "start", side_effect=[tunnel.SetupError("cannot start"), None]) as start:
            with self.assertRaises(tunnel.SetupError):
                tunnel.setup(self.options(data_dir=self.project / "runs"), self.layout)
        self.assertEqual(stop.call_count, 2)
        self.assertEqual(start.call_count, 2)
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)

    def test_stop_failure_does_not_replace_existing_key(self):
        with patch.object(tunnel, "job", return_value={"loaded": True, "pid": 12}), \
             patch.object(tunnel, "get_key", return_value="old-key"), \
             patch.object(tunnel, "prompt_key", return_value=SECRET), \
             patch.object(tunnel, "doctor"), patch.object(tunnel, "store_key") as store, \
             patch.object(tunnel, "stop", side_effect=tunnel.SetupError("stop failed")):
            with self.assertRaises(tunnel.SetupError):
                tunnel.setup(self.options(replace_key=True), self.layout)
        store.assert_not_called()

    def test_keyboard_interrupt_rolls_back_files_key_and_old_service(self):
        original = tunnel.render(TUNNEL, self.project / "acceptance", self.layout, self.project)
        tunnel.write_artifacts(original, self.layout)
        with patch.object(tunnel, "job", return_value={"loaded": True, "pid": 12}), \
             patch.object(tunnel, "get_key", return_value="old-key"), \
             patch.object(tunnel, "prompt_key", return_value=SECRET), \
             patch.object(tunnel, "doctor"), patch.object(tunnel, "stop"), \
             patch.object(tunnel, "store_key") as store, \
             patch.object(tunnel, "start", side_effect=[KeyboardInterrupt(), None]):
            with self.assertRaises(KeyboardInterrupt):
                tunnel.setup(self.options(replace_key=True), self.layout)
        self.assertEqual([call.args[0] for call in store.call_args_list], [SECRET, "old-key"])
        self.assertTrue(all(call.kwargs == {"update": True} for call in store.call_args_list))
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)

    def test_keyboard_interrupt_inside_stop_restarts_previous_job_without_key_change(self):
        original = tunnel.render(TUNNEL, self.project / "acceptance", self.layout, self.project)
        tunnel.write_artifacts(original, self.layout)
        with patch.object(tunnel, "job", side_effect=[{"loaded": True, "pid": 12},
                                                     {"loaded": False, "pid": None}]), \
             patch.object(tunnel, "get_key", return_value="old-key"), \
             patch.object(tunnel, "prompt_key", return_value=SECRET), \
             patch.object(tunnel, "doctor"), patch.object(tunnel, "stop", side_effect=KeyboardInterrupt()), \
             patch.object(tunnel, "store_key") as store, patch.object(tunnel, "start") as start:
            with self.assertRaises(KeyboardInterrupt):
                tunnel.setup(self.options(replace_key=True), self.layout)
        store.assert_not_called()
        start.assert_called_once_with(self.layout)
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)

    def test_doctor_and_runtime_receive_key_in_environment_never_argv(self):
        profile = tunnel.render(TUNNEL, self.project / "runs", self.layout, self.project)[self.layout["profile"]]
        with patch.object(tunnel, "command", return_value=subprocess.CompletedProcess([], 0, "", "")) as call:
            tunnel.doctor(profile, SECRET, self.layout)
        args, kwargs = call.call_args
        self.assertNotIn(SECRET, repr(args))
        self.assertEqual(kwargs["env"][tunnel.KEY_ENV], SECRET)
        with patch.object(tunnel, "get_key", return_value=SECRET), patch.object(tunnel.os, "execve") as execute:
            tunnel.run(self.layout)
        executable, argv, environment = execute.call_args.args
        self.assertEqual(executable, str(self.client))
        self.assertEqual(argv, tunnel.client_args("run", self.layout["profile"]))
        self.assertEqual(argv[-2:], ["--control-plane.initial-poll-timeout", "5s"])
        self.assertNotIn(SECRET, repr(argv))
        self.assertEqual(environment[tunnel.KEY_ENV], SECRET)
        self.assertFalse(list(self.layout["support"].glob(".check-*")))

    def test_private_key_file_modes_atomic_replacement_and_no_temporary_residue(self):
        tunnel.store_file_key(SECRET, self.layout)
        self.assertEqual(self.layout["support"].stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.layout["key_file"].stat().st_mode & 0o777, 0o600)
        self.assertEqual(tunnel.get_file_key(self.layout), SECRET)
        with patch.object(tunnel.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                tunnel.store_file_key("replacement", self.layout)
        self.assertEqual(tunnel.get_file_key(self.layout), SECRET)
        self.assertEqual(list(self.layout["support"].iterdir()), [self.layout["key_file"]])
        tunnel.store_file_key("replacement", self.layout)
        self.assertEqual(tunnel.get_file_key(self.layout), "replacement")

    def test_private_key_file_rejects_symlinks_hardlinks_and_public_modes(self):
        tunnel.store_file_key(SECRET, self.layout)
        self.layout["key_file"].chmod(0o644)
        with self.assertRaises(tunnel.SetupError):
            tunnel.get_file_key(self.layout)
        self.layout["key_file"].chmod(0o600)
        other = self.home / "other"
        os.link(self.layout["key_file"], other)
        with self.assertRaises(tunnel.SetupError):
            tunnel.store_file_key("replacement", self.layout)
        other.unlink()
        self.layout["key_file"].rename(other)
        self.layout["key_file"].symlink_to(other)
        with self.assertRaises(tunnel.SetupError):
            tunnel.store_file_key("replacement", self.layout)
        self.assertEqual(other.read_text(), SECRET)
        self.layout["key_file"].unlink()
        self.layout["support"].rmdir()
        destination = self.home / "redirected"
        destination.mkdir(mode=0o700)
        self.layout["support"].symlink_to(destination, target_is_directory=True)
        with self.assertRaises(tunnel.SetupError):
            tunnel.store_file_key(SECRET, self.layout)
        self.assertEqual(list(destination.iterdir()), [])

    def test_file_profile_and_runtime_have_no_keychain_or_secret_environment_dependency(self):
        tunnel.store_file_key(SECRET, self.layout)
        artifacts = tunnel.render(TUNNEL, self.project / "runs", self.layout, self.project, key_file=True)
        tunnel.write_artifacts(artifacts, self.layout)
        self.assertEqual(json.loads(artifacts[self.layout["profile"]])["control_plane"]["api_key"],
                         "file:" + str(self.layout["key_file"]))
        self.assertNotIn(SECRET, repr(artifacts))
        with patch.object(tunnel, "get_key") as keychain, patch.object(tunnel.os, "execve") as execute, \
             patch.dict(tunnel.os.environ, {tunnel.KEY_ENV: "unrelated", "OPENAI_API_KEY": "unrelated"}):
            tunnel.run(self.layout)
        keychain.assert_not_called()
        executable, argv, environment = execute.call_args.args
        self.assertEqual(executable, str(self.client))
        self.assertNotIn(SECRET, repr(argv))
        self.assertNotIn(tunnel.KEY_ENV, environment)
        self.assertNotIn("OPENAI_API_KEY", environment)
        with patch.object(tunnel, "job", return_value={"loaded": False, "pid": None}):
            report = tunnel.status(self.layout)
        self.assertEqual(report["credential_storage"], "file")
        self.assertNotIn(SECRET, repr(report))

    def test_file_migration_reads_existing_key_once_then_setup_reuses_file(self):
        report = {"loaded": True, "pid": 12, "healthy": True, "ready": True}
        with patch.object(tunnel, "job", return_value={"loaded": False, "pid": None}), \
             patch.object(tunnel, "get_key", return_value=SECRET) as keychain, \
             patch.object(tunnel, "store_key") as store, patch.object(tunnel, "prompt_key") as prompt, \
             patch.object(tunnel, "doctor"), patch.object(tunnel, "stop"), patch.object(tunnel, "start"), \
             patch.object(tunnel, "status", return_value=report):
            tunnel.setup(self.options(key_file=True), self.layout)
            keychain.assert_called_once_with()
            self.preflight.assert_called_once_with()
            keychain.reset_mock()
            self.preflight.reset_mock()
            tunnel.setup(self.options(data_dir=self.project / "runs"), self.layout)
            keychain.assert_not_called()
            self.preflight.assert_not_called()
        store.assert_not_called()
        prompt.assert_not_called()
        self.assertEqual(tunnel.get_file_key(self.layout), SECRET)
        self.assertEqual(tunnel.credential_storage(self.layout), "file")

    def test_failed_file_migration_restores_profile_and_removes_new_file(self):
        original = tunnel.render(TUNNEL, self.project / "acceptance", self.layout, self.project)
        tunnel.write_artifacts(original, self.layout)
        with patch.object(tunnel, "job", return_value={"loaded": True, "pid": 12}), \
             patch.object(tunnel, "get_key", return_value=SECRET), patch.object(tunnel, "store_key") as store, \
             patch.object(tunnel, "doctor"), patch.object(tunnel, "stop"), \
             patch.object(tunnel, "start", side_effect=[tunnel.SetupError("failed"), None]):
            with self.assertRaises(tunnel.SetupError):
                tunnel.setup(self.options(key_file=True), self.layout)
        store.assert_not_called()
        self.assertFalse(self.layout["key_file"].exists())
        for path, data in original.items():
            self.assertEqual(path.read_bytes(), data)

    def test_file_replacement_and_prepare_do_not_access_keychain(self):
        with patch.object(tunnel, "job", return_value={"loaded": False, "pid": None}), \
             patch.object(tunnel, "get_key") as keychain, patch.object(tunnel, "prompt_key", return_value=SECRET), \
             patch.object(tunnel, "doctor"), patch.object(tunnel, "stop"), patch.object(tunnel, "start"), \
             patch.object(tunnel, "status", return_value={"loaded": True, "pid": 12, "healthy": True, "ready": True}):
            tunnel.setup(self.options(key_file=True, prepare_only=True), self.layout)
            self.assertFalse(self.layout["key_file"].exists())
            tunnel.setup(self.options(key_file=True, replace_key=True), self.layout)
        keychain.assert_not_called()
        self.preflight.assert_not_called()


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("VOICE_RPG_TEST_KEYCHAIN") == "1",
                     "Opt-in macOS test: VOICE_RPG_TEST_KEYCHAIN=1; uses only a disposable synthetic item")
class NativeKeychainTests(unittest.TestCase):
    def test_synthetic_roundtrip_and_narrow_acl(self):
        # Never use the production item, even if the test aborts during storage.
        fixture_id = uuid.uuid4().hex
        service = "com.openai.voice-rpg.test." + fixture_id
        account = "synthetic-test-" + fixture_id
        tunnel.preflight_keychain()
        with patch.object(tunnel, "LABEL", service), patch.object(tunnel, "KEY_ACCOUNT", account):
            try:
                tunnel.store_key(SECRET)
                self.assertEqual(tunnel.get_key(), SECRET)
                replacement = SECRET + "-rotated"
                tunnel.store_key(replacement, update=True)
                self.assertEqual(tunnel.get_key(), replacement)
            finally:
                result = tunnel.command([tunnel.SECURITY, "delete-generic-password",
                                         "-a", account, "-s", service])
                self.assertIn(result.returncode, (0, 44), "Disposable Keychain fixture cleanup failed")
            self.assertIsNone(tunnel.get_key())


if __name__ == "__main__":
    unittest.main()
