#!/usr/bin/env python3
"""Configure only Voice RPG's user LaunchAgent and private MCP tunnel."""
import argparse
import getpass
import hmac
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import urlopen
import warnings

PROJECT = Path(__file__).resolve().parent
LABEL = "com.openai.voice-rpg.tunnel"
KEY_ACCOUNT = "voice-rpg-runtime"
KEY_ENV = "CONTROL_PLANE_API_KEY"
SECURITY = "/usr/bin/security"
LAUNCHCTL = "/bin/launchctl"
CLIENT = Path("/opt/homebrew/bin/tunnel-client")
INITIAL_POLL_TIMEOUT = "5s"
TUNNEL_ID = re.compile(r"tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}\Z")
KEYCHAIN_LINE_LIMIT = 4096  # Apple's security interactive parser buffer.
KEYCHAIN_UNLOCK_MESSAGE = (
    "The login Keychain is locked or requires user interaction. Run "
    "`/usr/bin/security unlock-keychain` in an interactive terminal and enter "
    "your Mac login/Keychain password at its hidden prompt, then rerun setup.")
KEYCHAIN_STATUS_SCRIPT = """
import ctypes as c
import sys
security = c.CDLL('/System/Library/Frameworks/Security.framework/Security')
core = c.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
security.SecKeychainCopyDefault.argtypes = [c.POINTER(c.c_void_p)]
security.SecKeychainCopyDefault.restype = c.c_int32
security.SecKeychainGetStatus.argtypes = [c.c_void_p, c.POINTER(c.c_uint32)]
security.SecKeychainGetStatus.restype = c.c_int32
core.CFRelease.argtypes = [c.c_void_p]
core.CFRelease.restype = None
keychain = c.c_void_p()
if security.SecKeychainCopyDefault(c.byref(keychain)) or not keychain.value:
    sys.exit(2)
try:
    flags = c.c_uint32()
    result = security.SecKeychainGetStatus(keychain, c.byref(flags))
    code = 2 if result else (0 if flags.value & 1 else 3)
finally:
    core.CFRelease(keychain)
sys.exit(code)
"""


class SetupError(Exception):
    pass


def paths(home=None):
    home = Path.home() if home is None else Path(home)
    support = home / "Library" / "Application Support" / "Voice RPG Tunnel"
    return {"support": support, "profile": support / "profile.json",
            "key_file": support / "runtime.key",
            "wrapper": support / "launch", "health": support / "health.url",
            "plist": home / "Library" / "LaunchAgents" / (LABEL + ".plist"),
            "log": home / "Library" / "Logs" / "Voice RPG Tunnel" / "tunnel.log"}


def command(args, **kwargs):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=30, **kwargs)
    except (OSError, subprocess.TimeoutExpired):
        # In particular, never include a subprocess exception's stdin or output.
        raise SetupError("A required system command failed or timed out.") from None


def preflight_keychain():
    # Read status only, in a bounded child; never unlock or inspect stored items.
    result = command([sys.executable, "-c", KEYCHAIN_STATUS_SCRIPT])
    if result.returncode == 3:
        raise SetupError(KEYCHAIN_UNLOCK_MESSAGE)
    if result.returncode:
        raise SetupError("The default macOS Keychain is unavailable. Sign in to the Mac and check Keychain Access, then retry.")


def keychain_error(result, fallback):
    # Match known diagnostics, but never expose raw security output (which may
    # include input data when its interactive parser rejects a command).
    diagnostic = result.stderr + result.stdout
    if "User interaction is not allowed" in diagnostic or "-25308" in diagnostic:
        raise SetupError(KEYCHAIN_UNLOCK_MESSAGE)
    raise SetupError(fallback)


def get_key():
    result = command([SECURITY, "find-generic-password", "-a", KEY_ACCOUNT, "-s", LABEL, "-w"])
    if result.returncode == 44:  # errSecItemNotFound
        return None
    if result.returncode:
        keychain_error(result, "The Voice RPG Keychain item could not be read. Check Keychain Access on the Mac and retry.")
    key = result.stdout.rstrip("\r\n")
    if not key:
        raise SetupError("The Voice RPG Keychain item is empty.")
    return key


def keychain_request(key, update=False):
    # security's interactive parser receives only fixed tokens and hexadecimal
    # data. Neither the secret nor its encoded form appears in process argv.
    if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise SetupError("The runtime key must be a nonempty single printable token.")
    encoded = key.encode("utf-8").hex()
    # Reapplying -T during an update invokes SecKeychainItemSetAccess, which
    # prompts even after the password has changed. Preserve the existing ACL.
    options = " -U" if update else " -T " + SECURITY
    request = ("add-generic-password" + options + " -a " + KEY_ACCOUNT + " -s " + LABEL
               + " -X " + encoded + "\n")
    if len(request.encode("ascii")) >= KEYCHAIN_LINE_LIMIT:
        raise SetupError("The runtime key is too long for macOS Keychain's command interface; no key was stored.")
    return request


def store_key(key, update=False):
    # EOF ends interactive mode; "quit" is not a security command.
    request = keychain_request(key, update=update)
    result = command([SECURITY, "-i"], input=request)
    if result.returncode:
        keychain_error(result, "The Voice RPG key could not be stored in Keychain. Check Keychain Access on the Mac and retry.")
    saved = get_key()
    if saved is None or not hmac.compare_digest(saved.encode("utf-8"), key.encode("utf-8")):
        raise SetupError("Keychain verification failed; the tunnel was not started.")


def prompt_key():
    if not sys.stdin.isatty():
        raise SetupError("A key is needed. Run setup yourself in a terminal with ssh -tt; input must be hidden.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            key = getpass.getpass("Tunnel runtime key (hidden): ").strip()
        except (getpass.GetPassWarning, EOFError):
            raise SetupError("A hidden terminal prompt is unavailable; no key was read.") from None
    keychain_request(key)
    return key


def private_key_path(layout, create=False):
    support = layout["support"]
    if any(path.is_symlink() for path in (support, *support.parents)):
        raise SetupError("The private credential directory must not contain symbolic links.")
    if create:
        support.mkdir(parents=True, exist_ok=True, mode=0o700)
    if support.exists():
        info = support.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise SetupError("The private credential directory must be owned by this user with mode 0700.")
    path = layout["key_file"]
    try:
        info = path.lstat()
    except FileNotFoundError:
        return path
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise SetupError("The runtime key must be a regular owner-only file with mode 0600 and no links.")
    return path


def get_file_key(layout):
    path = private_key_path(layout)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise SetupError("The runtime key file is not private; no credential was read.")
        data = stream.read(KEYCHAIN_LINE_LIMIT)
    try:
        key = data.decode("ascii")
    except UnicodeError:
        raise SetupError("The runtime key file does not contain a valid token.") from None
    keychain_request(key)
    return key


def store_file_key(key, layout):
    keychain_request(key)
    path = private_key_path(layout, create=True)
    atomic_write(path, key.encode("ascii"), 0o600)
    if not hmac.compare_digest(get_file_key(layout), key):
        raise SetupError("Runtime key file verification failed; the tunnel was not started.")


def render(tunnel_id, data_dir, layout, project=PROJECT, key_file=False):
    if not TUNNEL_ID.fullmatch(tunnel_id):
        raise SetupError("Use the complete tunnel ID returned by OpenAI.")
    project = project.resolve()
    data_dir = Path(data_dir).expanduser().resolve()
    profile = {"config_version": 1, "control_plane": {
        "base_url": "https://api.openai.com", "tunnel_id": tunnel_id,
        "api_key": "file:" + str(layout["key_file"]) if key_file else "env:" + KEY_ENV},
        "health": {"listen_addr": "127.0.0.1:0", "url_file": str(layout["health"])},
        "admin_ui": {"open_browser": False},
        "log": {"level": "info", "format": "json"},
        "mcp": {"stdio_send_initialized_notification": True,
                "commands": [{"channel": "main", "command": shlex.join([
            str(project / "rpg-mcp"), "--data-dir", str(data_dir)])}]}}
    # This launcher has no separate secret-handling implementation or game copy.
    wrapper = "#!/bin/sh\nexec " + shlex.join([
        str(project / ".venv" / "bin" / "python"),
        str(project / "install_tunnel.py"), "_run"]) + "\n"
    agent = {"Label": LABEL, "ProgramArguments": [str(layout["wrapper"])],
             "WorkingDirectory": str(project), "RunAtLoad": True, "KeepAlive": True,
             "ThrottleInterval": 30, "ProcessType": "Background",
             "StandardOutPath": str(layout["log"]), "StandardErrorPath": str(layout["log"])}
    return {layout["profile"]: (json.dumps(profile, indent=2) + "\n").encode("utf-8"),
            layout["wrapper"]: wrapper.encode("utf-8"),
            layout["plist"]: plistlib.dumps(agent)}


def read_profile(layout):
    if not layout["profile"].is_file():
        return None
    try:
        profile = json.loads(layout["profile"].read_text(encoding="utf-8"))
        if not isinstance(profile["control_plane"]["tunnel_id"], str):
            raise ValueError("Unexpected tunnel identifier")
        if profile["control_plane"]["api_key"] not in ("env:" + KEY_ENV, "file:" + str(layout["key_file"])):
            raise ValueError("Unexpected credential configuration")
        target = shlex.split(profile["mcp"]["commands"][0]["command"])
        if len(target) != 3 or target[1] != "--data-dir":
            raise ValueError("Unexpected target")
        return profile
    except (KeyError, ValueError, IndexError, TypeError):
        raise SetupError("The existing Voice RPG profile is not recognized; it was left untouched.") from None


def credential_storage(layout):
    profile = read_profile(layout)
    return "file" if profile and profile["control_plane"]["api_key"].startswith("file:") else "keychain"


def previous_settings(layout):
    profile = read_profile(layout)
    if profile is None:
        return None, None
    return profile["control_plane"]["tunnel_id"], Path(shlex.split(profile["mcp"]["commands"][0]["command"])[2])


def job():
    result = command([LAUNCHCTL, "print", "gui/%d/%s" % (os.getuid(), LABEL)])
    match = re.search(r"\bpid = ([0-9]+)", result.stdout) if not result.returncode else None
    return {"loaded": result.returncode == 0, "pid": int(match.group(1)) if match else None}


def stop():
    if not job()["loaded"]:
        return
    result = command([LAUNCHCTL, "bootout", "gui/%d/%s" % (os.getuid(), LABEL)])
    if result.returncode:
        raise SetupError("Could not stop Voice RPG's existing LaunchAgent; no replacement was started.")
    deadline = time.monotonic() + 10
    while job()["loaded"]:
        if time.monotonic() >= deadline:
            raise SetupError("Voice RPG's LaunchAgent has not stopped; no replacement was started.")
        time.sleep(0.2)


def start(layout):
    result = command([LAUNCHCTL, "bootstrap", "gui/%d" % os.getuid(), str(layout["plist"])])
    if result.returncode:
        raise SetupError("Could not start the Voice RPG LaunchAgent. A logged-in Mac desktop session is required.")


def status(layout):
    result = job()
    result.update({"healthy": False, "ready": False, "readiness_scope": "local_process",
                   "cloud_connection_verified": False, "profile": str(layout["profile"]),
                   "credential_storage": credential_storage(layout)})
    tunnel_id, data_dir = previous_settings(layout)
    result.update({"tunnel_id": tunnel_id, "data_dir": str(data_dir) if data_dir else None})
    if not result["pid"] or not layout["health"].is_file():
        return result
    base = layout["health"].read_text(encoding="utf-8").strip()
    parsed = urlsplit(base)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise SetupError("The Voice RPG health URL is not a loopback endpoint.")
    for endpoint, key in (("healthz", "healthy"), ("readyz", "ready")):
        try:
            with urlopen(base.rstrip("/") + "/" + endpoint, timeout=2) as response:
                result[key] = response.status == 200
        except (OSError, URLError):
            pass
    result["ui_url"] = base.rstrip("/") + "/ui"
    return result


def atomic_write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".voice-rpg-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_artifacts(artifacts, layout):
    layout["support"].mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(layout["support"], 0o700)
    layout["log"].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Launchd opens this file; precreate it with restrictive permissions.
    layout["log"].touch(exist_ok=True, mode=0o600)
    os.chmod(layout["log"], 0o600)
    for path, data in artifacts.items():
        atomic_write(path, data, 0o700 if path == layout["wrapper"] else 0o600)


def doctor(profile, key, layout):
    layout["support"].mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(prefix=".check-", suffix=".json", dir=str(layout["support"])) as staged:
        # Validate the candidate before changing either persistent credential store.
        candidate_profile = json.loads(profile)
        candidate_profile["control_plane"]["api_key"] = "env:" + KEY_ENV
        staged.write(json.dumps(candidate_profile).encode("utf-8"))
        staged.flush()
        environment = os.environ.copy()
        environment[KEY_ENV] = key
        result = command(client_args("doctor", staged.name) + ["--json"], env=environment)
    if result.returncode:
        raise SetupError("Tunnel checks failed; no service was changed. Check tunnel scope, network access, or use --replace-key for a new runtime key.")


def setup(args, layout):
    old_tunnel, old_data = previous_settings(layout)
    tunnel_id = args.tunnel_id or old_tunnel
    if not tunnel_id:
        raise SetupError("First setup requires --tunnel-id.")
    data_dir = args.data_dir or old_data or PROJECT / ".integration" / "acceptance-runs"
    file_mode = args.key_file or credential_storage(layout) == "file"
    if file_mode:
        private_key_path(layout)
    artifacts = render(tunnel_id, data_dir, layout, key_file=file_mode)
    if not CLIENT.is_file() or not (PROJECT / ".venv" / "bin" / "python").is_file():
        raise SetupError("Install the official tunnel-client and this repository's Python runtime first.")
    prior_job = job()
    if args.prepare_only:
        if prior_job["loaded"]:
            raise SetupError("Prepare-only will not replace an active service. Use setup to validate and switch it.")
        write_artifacts(artifacts, layout)
        return {"prepared": True, "started": False, "profile": str(layout["profile"]),
                "data_dir": str(Path(data_dir).expanduser().resolve()), "key_accessed": False}
    if file_mode:
        old_key = get_file_key(layout)
        candidate = old_key
        if args.replace_key:
            candidate = prompt_key()
        elif candidate is None:
            preflight_keychain()
            candidate = get_key()
        if candidate is None:
            candidate = prompt_key()
    else:
        preflight_keychain()
        old_key = get_key()
        candidate = prompt_key() if args.replace_key or old_key is None else old_key
    doctor(artifacts[layout["profile"]], candidate, layout)
    changed_key = candidate != old_key
    before = {path: (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
              for path in artifacts}
    stopped = False
    key_update_attempted = False
    try:
        stop()
        stopped = True
        if changed_key:
            key_update_attempted = True
            if file_mode:
                store_file_key(candidate, layout)
            else:
                store_key(candidate, update=old_key is not None)
        write_artifacts(artifacts, layout)
        layout["health"].unlink(missing_ok=True)
        start(layout)
        deadline = time.monotonic() + 30
        while True:
            report = status(layout)
            if report["loaded"] and report["pid"] and report["healthy"] and report["ready"]:
                return report
            if time.monotonic() >= deadline:
                raise SetupError("The new tunnel did not become ready; its service was rolled back.")
            time.sleep(0.5)
    except (Exception, KeyboardInterrupt):
        rollback_ok = True
        if stopped:
            try:
                stop()
                for path, original in before.items():
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write(path, original[0], original[1])
                layout["health"].unlink(missing_ok=True)
                if key_update_attempted:
                    if file_mode:
                        if old_key is None:
                            private_key_path(layout).unlink(missing_ok=True)
                        else:
                            store_file_key(old_key, layout)
                    elif old_key is not None:
                        store_key(old_key, update=True)
                if prior_job["loaded"]:
                    start(layout)
            except (Exception, KeyboardInterrupt):
                rollback_ok = False
        elif prior_job["loaded"]:
            # bootout may finish before an interrupt reaches stop's polling loop.
            # No files or keys changed yet; restore only the prior loaded job.
            try:
                if not job()["loaded"]:
                    start(layout)
            except (Exception, KeyboardInterrupt):
                rollback_ok = False
        if not rollback_ok:
            raise SetupError("Setup failed and rollback could not finish. The Voice RPG LaunchAgent needs attention; no other service was changed.") from None
        raise


def client_args(operation, profile):
    # The installed client exposes this through CLI/env, not its strict YAML schema.
    return [str(CLIENT), operation, "--config", str(profile),
            "--control-plane.initial-poll-timeout", INITIAL_POLL_TIMEOUT]


def run(layout):
    environment = os.environ.copy()
    if credential_storage(layout) == "file":
        path = private_key_path(layout)
        if not path.is_file():
            raise SetupError("Voice RPG's private runtime key file is missing. Run setup in a terminal.")
        # The supported client reads the file reference directly. Inherited API
        # key variables must not override this explicit credential selection.
        environment.pop(KEY_ENV, None)
        environment.pop("OPENAI_API_KEY", None)
    else:
        key = get_key()
        if key is None:
            raise SetupError("Voice RPG's runtime key is missing. Run install_tunnel.py setup in a terminal.")
        environment[KEY_ENV] = key
    os.execve(str(CLIENT), client_args("run", layout["profile"]), environment)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    install = subs.add_parser("setup", help="Validate, configure and start this user's Voice RPG tunnel")
    install.add_argument("--tunnel-id")
    install.add_argument("--data-dir", type=Path, help="Collection to serve; retains current selection, initially isolated acceptance-runs")
    install.add_argument("--prepare-only", action="store_true", help="Write non-secret configuration only; no Keychain access or service start")
    install.add_argument("--replace-key", action="store_true", help="Prompt privately for a replacement runtime key")
    install.add_argument("--key-file", action="store_true", help="Use an owner-only runtime.key outside the repository; migrate the saved Keychain key once if needed")
    subs.add_parser("status", help="Show this service's process, collection and loopback readiness without reading Keychain")
    subs.add_parser("stop", help="Stop only this user's Voice RPG tunnel")
    subs.add_parser("_run", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if sys.platform != "darwin":
            raise SetupError("This helper requires macOS launchd.")
        layout = paths()
        if args.command == "setup":
            result = setup(args, layout)
        elif args.command == "status":
            result = status(layout)
        elif args.command == "stop":
            stop()
            result = {"stopped": True}
        else:
            run(layout)
            return 0
        print(json.dumps(result))
        return 0
    except (SetupError, OSError, ValueError) as exc:
        # Error messages here are authored by this helper, never raw subprocess output.
        print("Voice RPG tunnel: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Voice RPG tunnel: setup cancelled.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
