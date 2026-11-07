#!/usr/bin/env python3
"""Install the optional MCP runtime; --codex also registers a thin local plugin."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

from voice_rpg.contracts import VERSION

PROJECT = Path(__file__).resolve().parent
MARKETPLACE = "voice-rpg-local"
PLUGIN = "voice-rpg"


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def prepare_plugin(project=PROJECT):
    project = project.resolve()
    root = project / ".integration" / "marketplace"
    plugin = root / "plugins" / PLUGIN
    # The shipped app-server recognizes this explicit compatibility wiring.
    # Its portable manifest reader currently omits the otherwise valid mcp.json.
    for obsolete in ("plugin.json", "mcp.json"):
        (plugin / obsolete).unlink(missing_ok=True)
    write_json(plugin / ".codex-plugin" / "plugin.json", {
        "name": PLUGIN, "version": VERSION,
        "description": "Persistent voice adventure tools backed by this computer's authoritative game collection.",
        "mcpServers": "./.mcp.json", "requires_local_executor": True,
        "interface": {"displayName": "Voice RPG"}})
    write_json(plugin / ".mcp.json", {"mcpServers": {"voice-rpg": {
        "command": str(project / "rpg-mcp"),
        "args": ["--data-dir", str(project / "runs")], "cwd": str(project)}}})
    write_json(root / ".agents" / "plugins" / "marketplace.json", {
        "name": MARKETPLACE, "interface": {"displayName": "Local Voice RPG"},
        "plugins": [{"name": PLUGIN, "source": {"source": "local", "path": "./plugins/" + PLUGIN},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Productivity"}]})
    return root


def retire_legacy_links(project=PROJECT, home=None, codex_home=None):
    """Only remove known links to this repository; preserve real directories and other installs."""
    home = Path.home() if home is None else Path(home)
    codex_home = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) if codex_home is None else Path(codex_home)
    source = (project / "skills" / "voice-rpg").resolve()
    removed = []
    for link in (home / ".agents" / "skills" / PLUGIN, codex_home / "skills" / PLUGIN):
        if link.is_symlink() and link.resolve() == source:
            link.unlink()
            removed.append(str(link))
    return removed


def find_codex(explicit=None):
    if explicit:
        candidate = shutil.which(explicit)
        if candidate:
            return candidate
        raise ValueError("Codex CLI is not executable: " + explicit)
    bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
    if bundled.is_file() and os.access(str(bundled), os.X_OK):
        return str(bundled)
    candidate = shutil.which("codex")
    if candidate:
        return candidate
    raise ValueError("Codex CLI was not found. Supply --codex PATH or use --runtime-only.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--runtime-only", action="store_true", help="Install only MCP dependencies (the default)")
    modes.add_argument("--prepare-only", action="store_true", help="Generate the thin plugin package without changing runtime or app configuration")
    parser.add_argument("--codex", help="Path to the supported Codex CLI used for local plugin installation")
    args = parser.parse_args(argv)
    if args.codex and (args.runtime_only or args.prepare_only):
        parser.error("--codex cannot be combined with --runtime-only or --prepare-only")
    try:
        if args.prepare_only:
            root = prepare_plugin()
            print(json.dumps({"prepared": str(root), "registered": False}))
            return 0
        if sys.version_info < (3, 12):
            raise ValueError("Use Python 3.12 or newer to install the pinned MCP runtime.")
        # Resolve the installation path before spending time on dependency setup.
        codex = find_codex(args.codex) if args.codex else None
        environment = PROJECT / ".venv"
        if not (environment / "bin" / "python").is_file():
            venv.EnvBuilder(with_pip=True).create(str(environment))
        python = str(environment / "bin" / "python")
        subprocess.run([python, "-m", "pip", "install", "--disable-pip-version-check",
                        "-r", str(PROJECT / "requirements.lock")], check=True)
        subprocess.run([python, "-m", "pip", "check"], check=True)
        subprocess.run([python, "-c", "import mcp; from voice_rpg.mcp import build_server"],
                       cwd=str(PROJECT), check=True)
        result = {"runtime": str(environment), "database": str(PROJECT / "runs" / "game.sqlite"),
                  "registered": False}
        if codex is not None:
            root = prepare_plugin()
            subprocess.run([codex, "plugin", "marketplace", "add", str(root), "--json"], check=True)
            subprocess.run([codex, "plugin", "add", PLUGIN + "@" + MARKETPLACE, "--json"], check=True)
            result["registered"] = True
            result["retired_legacy_skill_links"] = retire_legacy_links()
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print("Installation failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
