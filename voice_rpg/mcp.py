"""Thin stdio MCP transport. Schemas, game behavior and instructions live elsewhere."""
import argparse
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import time
import uuid

import anyio
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool, ToolAnnotations

from .contracts import GameError, SCHEMAS, VERSION
from .engine import Engine
from .guide import PROJECT, load_guide

LOG = logging.getLogger("voice_rpg.mcp")
READ_COMMANDS = frozenset(("resume", "lookup"))


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _operation_view(variants):
    """Present the same operations without ChatGPT's opaque nested union."""
    names = [variant["properties"]["op"]["const"] for variant in variants]
    fields = {}
    for variant in variants:
        for name, schema in variant["properties"].items():
            if name != "op":
                fields.setdefault(name, [])
                if schema not in fields[name]:
                    fields[name].append(schema)
    props = {"op": {"type": "string", "enum": names}}
    meanings = [name + ": " + variant["description"] for name, variant in zip(names, variants)
                if variant.get("description")]
    if meanings:
        props["op"]["description"] = " ".join(meanings)
    for name, options in fields.items():
        schema = copy.deepcopy(options[0]) if len(options) == 1 else {"anyOf": copy.deepcopy(options)}
        required = [op for op, variant in zip(names, variants) if name in variant["required"]]
        optional = [op for op, variant in zip(names, variants)
                    if name in variant["properties"] and name not in variant["required"]]
        usage = []
        if required:
            usage.append("Required for " + ", ".join(required) + ".")
        if optional:
            usage.append("Optional for " + ", ".join(optional) + ".")
        schema["description"] = " ".join(usage + ([schema["description"]] if schema.get("description") else []))
        props[name] = schema
    return {"type": "object", "properties": props, "required": ["op"], "additionalProperties": False}


def _connector_schema(schema):
    """Adapt presentation quirks without weakening the engine's own validation."""
    projected = copy.deepcopy(schema)

    def visit(node):
        if isinstance(node, dict):
            node.pop("pattern", None)
            # Both nested oneOf and anyOf rendered as unknown in the connector.
            # Field usage is derived here; exact branch validation stays canonical.
            variants = node.get("oneOf", [])
            if variants and all("op" in variant.get("properties", {}) for variant in variants):
                node.clear()
                node.update(_operation_view(variants))
            for key, value in node.items():
                if key in ("properties", "patternProperties", "$defs", "definitions"):
                    for child in value.values():
                        visit(child)
                elif key not in ("const", "enum", "default", "examples"):
                    visit(value)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(projected)
    return projected


def _connector_description(schema):
    """Keep operation contracts visible when a client drops nested field help."""
    description = schema["description"]
    variants = schema.get("properties", {}).get("changes", {}).get("items", {}).get("oneOf", [])
    if not variants or not all("const" in variant.get("properties", {}).get("op", {})
                               for variant in variants):
        return description
    signatures, meanings, field_help, defaults = [], [], {}, {}

    def collect_help(node, path):
        if node.get("description"):
            paths = field_help.setdefault(node["description"], [])
            if path not in paths:
                paths.append(path)
        for name, child in node.get("properties", {}).items():
            collect_help(child, path + "." + name)
        for union in ("oneOf", "anyOf"):
            for child in node.get(union, []):
                collect_help(child, path)

    for variant in variants:
        name = variant["properties"]["op"]["const"]
        fields = []
        for field, contract in variant["properties"].items():
            if field == "op":
                continue
            fields.append(field + ("" if field in variant["required"] else "?"))
            collect_help(contract, field)
            if "default" in contract:
                defaults.setdefault(field, set()).add(_encode(contract["default"]))
        signatures.append(name + "(" + ",".join(fields) + ")")
        if variant.get("description"):
            meanings.append(name + ": " + variant["description"])
    details = [", ".join(paths) + ": " + help_text for help_text, paths in field_help.items()]
    details.extend(field + " defaults to " + next(iter(values)) + "."
                   for field, values in defaults.items() if len(values) == 1)
    return " ".join([description,
        "Each changes entry contains op and only that operation's fields (? marks optional): " +
        "; ".join(signatures) + ".", *meanings, *details])


def build_server(engine):
    """Bind one collection without expanding input defaults or caching game results."""
    names = {"rpg_" + command: command for command in SCHEMAS}
    schemas = {command: _connector_schema(schema) for command, schema in SCHEMAS.items()}

    async def list_tools(context, params):
        return ListToolsResult(tools=[Tool(
            name=name, description=_connector_description(SCHEMAS[command]),
            input_schema=schemas[command],
            annotations=ToolAnnotations(
                read_only_hint=command in READ_COMMANDS,
                destructive_hint=command in ("act", "commit", "cancel", "correct"),
                idempotent_hint=True, open_world_hint=False))
            for name, command in names.items()])

    async def call_tool(context, params):
        invocation = uuid.uuid4().hex
        started = time.monotonic()
        command = names.get(params.name)
        arguments = params.arguments if params.arguments is not None else {}
        # Canonical bytes identify identical retries without storing their text.
        # Malformed values must reach validation even if they cannot be measured.
        try:
            measured = json.dumps(arguments, sort_keys=True, ensure_ascii=True,
                                  allow_nan=False, separators=(",", ":")).encode("ascii")
        except (ValueError, TypeError, RecursionError):
            measured = None
        LOG.info(_encode({"event": "tool_start", "at": _timestamp(),
            "invocation": invocation, "tool": params.name if command else "unknown",
            "version": VERSION,
            "argument_bytes": len(measured) if measured is not None else None,
            "request_digest": hashlib.sha256(measured).hexdigest()[:24] if measured is not None else None}))
        failed = False
        try:
            if command is None:
                raise GameError("unknown_tool", "Unknown RPG tool.")
            result = await anyio.to_thread.run_sync(engine.dispatch, command, arguments)
        except GameError as exc:
            failed = True
            result = {"error": {"code": exc.code, "message": exc.message, "details": exc.details}}
        except (OSError, sqlite3.Error) as exc:
            failed = True
            result = {"error": {"code": "storage_error", "message": str(exc), "details": {}}}
        except Exception as exc:
            failed = True
            LOG.error("Unexpected engine failure: %s", type(exc).__name__)
            result = {"error": {"code": "internal_error", "message":
                "Unexpected runtime error. Inspect saved action status before retrying.",
                "details": {"type": type(exc).__name__}}}
        encoded = _encode(result)
        # Operational correlation only: do not put fiction, arguments or secrets in logs.
        LOG.info(_encode({"event": "tool_end", "at": _timestamp(), "invocation": invocation,
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "result_bytes": len(encoded.encode("utf-8")), "error": failed,
            "error_code": result.get("error", {}).get("code"),
            "warning_code": result.get("warning", {}).get("code"),
            "replayed": result.get("replayed", False),
            "accepted_revision": result.get("accepted_revision"),
            "current_revision": result.get("current_revision")}))
        return CallToolResult(content=[TextContent(type="text", text=encoded)],
                              structured_content=result, is_error=failed)

    def input_schema(name):
        command = names.get(name)
        return schemas[command] if command else None

    return Server("voice-rpg", version=VERSION, instructions=load_guide(),
                  on_list_tools=list_tools, on_call_tool=call_tool,
                  get_tool_input_schema=input_schema)


async def serve(data_dir):
    server = build_server(Engine(data_dir))
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Voice RPG stdio MCP server")
    parser.add_argument("--data-dir", type=Path, default=PROJECT / "runs")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    LOG.setLevel(logging.INFO)
    try:
        asyncio.run(serve(args.data_dir.expanduser().resolve()))
    except (GameError, OSError, sqlite3.Error, ValueError) as exc:
        LOG.error("Voice RPG startup failed: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
