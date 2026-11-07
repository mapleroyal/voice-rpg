"""Maintenance and JSON developer access to the same engine used by MCP."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import sqlite3
import sys

from .contracts import GameError, SCHEMAS, VERSION, SAVE_VERSION, MAX_JSON_BYTES, validate
from .engine import Engine
from .guide import PROJECT, GUIDE_PATH, load_guide
from .rules import RULES

UTILITY_COMMANDS = ("schema", "guide", "doctor", "backup", "restore")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("Non-finite JSON number: " + value)


def read_input(path):
    if path:
        with open(path, "rb") as stream:
            raw = stream.read(MAX_JSON_BYTES + 1)
    elif sys.stdin.isatty():
        raw = b""
    else:
        raw = sys.stdin.buffer.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("Input exceeds %d bytes." % MAX_JSON_BYTES)
    return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                      parse_constant=reject_constant) if raw.strip() else {}


def doctor(data_dir):
    """Inspect this runtime without creating a database or printing configuration secrets."""
    existing = data_dir
    while not existing.exists():
        existing = existing.parent
    database = data_dir / "game.sqlite"
    try:
        sdk_version = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        sdk_version = None
    result = {"version": VERSION, "save_version": SAVE_VERSION,
        "rules": {"id": RULES["id"], "version": RULES["version"]},
        "python": sys.version.split()[0], "interpreter": str(Path(sys.executable).absolute()),
        "sqlite": sqlite3.sqlite_version, "mcp_sdk": sdk_version,
        "project": str(PROJECT), "data_dir": str(data_dir), "database": str(database),
        "database_exists": database.exists(), "data_parent_writable": os.access(str(existing), os.W_OK),
        "guide": str(GUIDE_PATH), "guide_readable": GUIDE_PATH.is_file(),
        "tools": ["rpg_" + command for command in SCHEMAS]}
    if database.is_file():
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            result["database_schema_version"] = connection.execute("PRAGMA user_version").fetchone()[0]
            result["database_quick_check"] = connection.execute("PRAGMA quick_check").fetchone()[0]
            result["database_compatible"] = result["database_schema_version"] == SAVE_VERSION
        finally:
            connection.close()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Voice RPG: maintenance and JSON access to the game engine.",
        epilog="Gameplay reads one JSON object from stdin or --input FILE. Stop all game writers before restore.")
    parser.add_argument("command", nargs="?", choices=sorted(SCHEMAS) + list(UTILITY_COMMANDS))
    parser.add_argument("subject", nargs="?", help="Gameplay command name for schema, or SQLite backup path for restore")
    parser.add_argument("--input", metavar="FILE", help="Read one JSON request from a file")
    parser.add_argument("--data-dir", type=Path, default=PROJECT / "runs", help="Collection directory (default: project/runs)")
    parser.add_argument("--pretty", action="store_true", help="Indent JSON output")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_intermixed_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.subject and args.command not in ("schema", "restore"):
        parser.error("A second positional argument is only valid with schema or restore")
    if args.command == "restore" and not args.subject:
        parser.error("restore requires a SQLite backup path; stop all writers first")
    if args.input and args.command not in SCHEMAS:
        parser.error("--input is only valid for gameplay commands")
    exit_code = 0
    try:
        data_dir = args.data_dir.expanduser().resolve()
        if args.command == "guide":
            print(load_guide(), end="")
            return 0
        if args.command == "schema":
            if args.subject:
                if args.subject not in SCHEMAS:
                    if args.subject in UTILITY_COMMANDS:
                        raise ValueError(args.subject + " is a CLI utility without a JSON gameplay schema; run rpg " + args.subject + " directly.")
                    raise ValueError("Unknown gameplay command: " + args.subject + "; run rpg schema to list valid commands.")
                result = {"command": args.subject, "input_schema": SCHEMAS[args.subject]}
            else:
                result = {"version": VERSION, "commands": {
                    name: schema["description"] for name, schema in SCHEMAS.items()}}
        elif args.command == "doctor":
            result = doctor(data_dir)
        elif args.command == "backup":
            result = {"backup": str(Engine(data_dir).backup())}
        elif args.command == "restore":
            result = Engine(data_dir, initialize=False).restore(Path(args.subject).expanduser().resolve())
        else:
            payload = read_input(args.input)
            validate(args.command, payload)
            result = Engine(data_dir).dispatch(args.command, payload)
        response = {"ok": True, "result": result}
    except GameError as exc:
        response = {"ok": False, "error": {"code": exc.code, "message": exc.message, "details": exc.details}}
        exit_code = 1
    except (ValueError, UnicodeError, RecursionError) as exc:
        response = {"ok": False, "error": {"code": "invalid_input", "message": str(exc), "details": {}}}
        exit_code = 1
    except (OSError, sqlite3.Error) as exc:
        response = {"ok": False, "error": {"code": "storage_error", "message": str(exc), "details": {}}}
        exit_code = 1
    except Exception as exc:
        response = {"ok": False, "error": {"code": "internal_error",
            "message": "Unexpected runtime error; inspect saved action status before retrying.",
            "details": {"type": type(exc).__name__}}}
        if os.environ.get("VOICE_RPG_DEBUG") == "1":
            import traceback
            traceback.print_exc(file=sys.stderr)
        exit_code = 1
    print(json.dumps(response, ensure_ascii=False, allow_nan=False,
                     indent=2 if args.pretty else None, separators=None if args.pretty else (",", ":")))
    return exit_code
