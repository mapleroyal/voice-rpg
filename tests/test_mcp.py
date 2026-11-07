"""Actual stdio protocol tests. Run with the pinned SDK environment."""
import copy
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager

from voice_rpg.contracts import HOLDER_SCHEMA, OPERATION_SCHEMA, SCHEMAS, VERSION
from voice_rpg.guide import load_guide

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from voice_rpg.mcp import _connector_description, _connector_schema
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False

PROJECT = Path(__file__).resolve().parents[1]


def creation():
    return {"campaign": "stdio-test", "request_id": "create", "title": "Stdio Adventure",
            "preferences": {}, "character": {"concept": "A courier",
                "motivation": "Deliver a letter", "talents": ["notice", "climb", "talk"]},
            "opening": {"situation": "A narrow bridge crosses the gorge."}}


@unittest.skipUnless(HAVE_MCP, "MCP SDK missing; run these tests in the installed .venv")
class MCPTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "runs"
        self.logs = Path(self.tmp.name) / "stderr.log"

    def test_schema_projection_preserves_data_and_property_names(self):
        schema = {"type": "object", "properties": {"pattern": {
            "type": "string", "pattern": r"\S", "default": "Bell"}},
            "const": {"pattern": "literal data"}, "default": {"pattern": "Diver"}}
        original = copy.deepcopy(schema)
        projected = _connector_schema(schema)
        self.assertEqual(schema, original)
        self.assertEqual(projected["properties"]["pattern"], {"type": "string", "default": "Bell"})
        self.assertEqual(projected["const"], original["const"])
        self.assertEqual(projected["default"], original["default"])

    def test_visible_operation_help_tracks_contract_changes(self):
        schema = copy.deepcopy(SCHEMAS["act"])
        variants = schema["properties"]["changes"]["items"]["oneOf"]
        transfer = next(option for option in variants if option["properties"]["op"]["const"] == "transfer_item")
        transfer["properties"]["op"]["const"] = "relocate"
        transfer["required"].remove("quantity")
        original = copy.deepcopy(schema)
        description = _connector_description(schema)
        self.assertIn("relocate(id,to,quantity?,expected_holder?)", description)
        self.assertNotIn("transfer_item(", description)
        self.assertEqual(description.count(HOLDER_SCHEMA["description"]), 1)
        self.assertEqual(schema, original)

    @asynccontextmanager
    async def session(self):
        params = StdioServerParameters(command=sys.executable,
            args=["-m", "voice_rpg.mcp", "--data-dir", str(self.data)], cwd=str(PROJECT))
        with self.logs.open("a", encoding="utf-8") as errors:
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=15) as session:
                    initialized = await session.initialize()
                    yield session, initialized

    async def call(self, session, name, args, error=False):
        result = await session.call_tool("rpg_" + name, args)
        self.assertEqual(bool(result.is_error), error, result)
        self.assertEqual(result.structured_content, json.loads(result.content[0].text))
        return result.structured_content

    async def test_discovery_canonical_instructions_schemas_and_honest_annotations(self):
        canonical = copy.deepcopy(SCHEMAS)

        def compare(advertised, contract):
            if isinstance(contract, dict):
                keys = set(contract) - {"pattern"}
                if contract == OPERATION_SCHEMA:
                    self.assertEqual(advertised["type"], "object")
                    self.assertEqual(advertised["required"], ["op"])
                    self.assertFalse(advertised["additionalProperties"])
                    self.assertEqual(advertised["properties"]["op"]["enum"],
                        [option["properties"]["op"]["const"] for option in contract["oneOf"]])
                    self.assertEqual(set(advertised["properties"]),
                        {field for option in contract["oneOf"] for field in option["properties"]})
                    for option in contract["oneOf"]:
                        op = option["properties"]["op"]["const"]
                        for field, original in option["properties"].items():
                            if field == "op":
                                continue
                            actual = copy.deepcopy(advertised["properties"][field])
                            self.assertIn(op, actual["description"])
                            del actual["description"]
                            expected = _connector_schema(original)
                            expected.pop("description", None)
                            self.assertIn(expected, actual.get("anyOf", [actual]))
                    return
                self.assertEqual(set(advertised), keys)
                for key in advertised:
                    compare(advertised[key], contract[key])
            elif isinstance(contract, list):
                self.assertEqual(len(advertised), len(contract))
                for left, right in zip(advertised, contract):
                    compare(left, right)
            else:
                self.assertEqual(advertised, contract)

        async with self.session() as (session, initialized):
            self.assertEqual(initialized.server_info.version, VERSION)
            self.assertEqual(initialized.instructions, load_guide())
            tools = (await session.list_tools()).tools
            self.assertEqual({tool.name for tool in tools}, {"rpg_" + name for name in SCHEMAS})
            for tool in tools:
                command = tool.name.removeprefix("rpg_")
                compare(tool.input_schema, SCHEMAS[command])
                if command in ("act", "commit"):
                    self.assertTrue(tool.description.startswith(SCHEMAS[command]["description"] + " "))
                    self.assertIn("Each changes entry contains op and only that operation's fields", tool.description)
                    self.assertIn("? marks optional", tool.description)
                    self.assertIn("transfer_item(id,to,quantity,expected_holder?)", tool.description)
                    self.assertIn("advance(id,talent)", tool.description)
                    self.assertNotIn("goal_id", tool.description)
                    self.assertEqual(tool.description.count(HOLDER_SCHEMA["description"]), 1)
                    self.assertIn('expected_holder defaults to "pc".', tool.description)
                    for option in OPERATION_SCHEMA["oneOf"]:
                        name = option["properties"]["op"]["const"]
                        self.assertEqual(tool.description.count(name + "("), 1)
                        if option.get("description"):
                            self.assertIn(name + ": " + option["description"], tool.description)
                else:
                    self.assertEqual(tool.description, SCHEMAS[command]["description"])
                self.assertEqual(tool.annotations.read_only_hint, command in ("resume", "lookup"))
                self.assertFalse(tool.annotations.open_world_hint)
                self.assertTrue(tool.annotations.idempotent_hint)
            self.assertIsNone(next(tool for tool in tools if tool.name == "rpg_begin").output_schema)
            self.assertFalse(next(tool for tool in tools if tool.name == "rpg_begin").annotations.read_only_hint)
        self.assertEqual(SCHEMAS, canonical)

    async def test_inventory_handover_errors_retry_and_ownership(self):
        args = creation()
        args["opening"]["records"] = [{"kind": "npc", "id": "porter", "name": "Porter"}]
        args["possessions"] = [{"template": "item", "id": "parcel", "name": "Parcel"}]
        request = {"campaign": args["campaign"], "expected_revision": 0,
                   "request_id": "handover", "changes": []}
        async with self.session() as (session, initialized):
            await self.call(session, "create", args)
            for operation in [
                {"op": "transfer", "id": "parcel", "holder": "porter"},
                {"op": "transfer_item", "id": "parcel", "to": "porter"},
                {"op": "transfer_item", "id": "parcel", "to": "porter", "quantity": 1, "holder": "porter"},
            ]:
                result = await self.call(session, "act", {**request, "changes": [operation]}, error=True)
                self.assertEqual(result["error"]["code"], "invalid_input")
            before = await self.call(session, "resume", {"campaign": args["campaign"]})
            self.assertEqual(before["campaign"]["revision"], 0)
            self.assertEqual(before["inventory"][0]["holder"], "pc")
            request["changes"] = [{"op": "transfer_item", "id": "parcel", "to": "porter",
                                   "quantity": 1, "expected_holder": "pc"}]
            accepted = await self.call(session, "act", request)
            self.assertEqual(accepted["current_revision"], 1)
            self.assertTrue((await self.call(session, "act", request))["replayed"])
            held = await self.call(session, "lookup", {"campaign": args["campaign"],
                "selector": {"by": "holder", "holder": "porter"}})
            self.assertEqual([(item["id"], item["quantity"]) for item in held["records"]], [("parcel", 1)])

    async def test_connector_strings_reach_strict_engine_validation(self):
        args = creation()
        args.update(title="Bell", preferences={"genre": "Mystery"})
        args["character"]["concept"] = "Diver"
        async with self.session() as (session, initialized):
            for path, invalid in [
                (("request_id",), " \t\n"), (("title",), " \t"),
                (("preferences", "genre"), "\u2003"),
                (("character", "talents", 1), " "),
                *((('campaign',), value) for value in ("", "Uppercase", "a/b", "valid\n")),
            ]:
                with self.subTest(path=path, invalid=invalid):
                    bad = copy.deepcopy(args)
                    parent = bad
                    for key in path[:-1]:
                        parent = parent[key]
                    parent[path[-1]] = invalid
                    result = await self.call(session, "create", bad, error=True)
                    self.assertEqual(result["error"]["code"], "invalid_input")
                    self.assertEqual((await self.call(session, "resume", {}))["campaigns"], [])
            created = await self.call(session, "create", args)
            self.assertEqual(created["current_revision"], 0)
            self.assertTrue((await self.call(session, "create", args))["replayed"])
            result = await self.call(session, "act", {"campaign": args["campaign"],
                "expected_revision": 0, "request_id": "bad-beat", "changes": [], "beat": " \n"}, error=True)
            self.assertEqual(result["error"]["code"], "invalid_input")
            self.assertEqual((await self.call(session, "resume", {}))["campaign"]["revision"], 0)

    async def test_real_calls_restart_deduplication_and_recovery(self):
        begin = {"campaign": "stdio-test", "expected_revision": 0, "request_id": "cross",
                 "intent": "Cross the gorge", "stakes": "Risk falling", "talent": "climb"}
        async with self.session() as (session, initialized):
            await self.call(session, "create", creation())
            unknown_talent = {**begin, "request_id": "typo", "talent": "clmib"}
            failed = await self.call(session, "begin", unknown_talent, error=True)
            self.assertIn("code", failed["error"])
            begun = await self.call(session, "begin", begin)
        async with self.session() as (session, initialized):
            recovered = await self.call(session, "begin", begin)
            self.assertTrue(recovered["replayed"])
            self.assertEqual(recovered["action_id"], begun["action_id"])
            self.assertEqual(recovered["check"]["dice"], begun["check"]["dice"])
            args = {"campaign": "stdio-test", "action_id": begun["action_id"],
                    "changes": [{"op": "harm", "severity": "minor"}], "beat": "Mara reaches the ledge, bruised."}
            completed = await self.call(session, "commit", args)
            replayed = await self.call(session, "commit", args)
            self.assertTrue(replayed["replayed"])
            self.assertEqual(replayed["current_revision"], completed["current_revision"])
            current = await self.call(session, "lookup", {"campaign": "stdio-test", "selector": {"by": "ids", "ids": ["pc"]}})
            self.assertEqual(current["records"][0]["health"], 5)
            historic = await self.call(session, "begin", begin)
            self.assertTrue(historic["refresh_required"])
            self.assertNotIn("changes", historic)
            self.assertEqual(historic["status"], "completed")
        events = [json.loads(line) for line in self.logs.read_text().splitlines() if line.startswith("{")]
        starts = {event["invocation"]: event for event in events if event["event"] == "tool_start"}
        ends = {event["invocation"]: event for event in events if event["event"] == "tool_end"}
        self.assertGreaterEqual(len(starts), 8)
        self.assertEqual(len(starts) + len(ends), len(events))
        self.assertEqual(starts.keys(), ends.keys())
        for invocation, event in ends.items():
            self.assertGreaterEqual(event["elapsed_ms"], 0)
            self.assertTrue(event["at"].endswith("Z"))
            self.assertGreaterEqual(datetime.fromisoformat(event["at"]),
                                    datetime.fromisoformat(starts[invocation]["at"]))
            self.assertEqual(starts[invocation]["version"], VERSION)
        self.assertTrue(any(event["error_code"] for event in ends.values()))
        begin_events = [event for event in starts.values() if event["tool"] == "rpg_begin"]
        self.assertEqual(len({event["request_digest"] for event in begin_events}), 2)
        self.assertTrue(any(event["replayed"] for event in ends.values()))
        self.assertNotIn("Cross the gorge", self.logs.read_text())
        self.assertNotIn("Mara reaches", self.logs.read_text())
        self.assertNotIn("clmib", self.logs.read_text())

    async def test_transport_errors_preserve_pending_and_resume_delivers_the_same_guide(self):
        async with self.session() as (session, initialized):
            empty = await self.call(session, "resume", {})
            self.assertEqual(empty, {"campaigns": [], "guide": load_guide()})
            self.assertEqual(await self.call(session, "resume", {"include_guide": False}), {"campaigns": []})
            await self.call(session, "create", creation())
            begun = await self.call(session, "begin", {"campaign": "stdio-test", "expected_revision": 0,
                "request_id": "try", "intent": "Cross the bridge", "stakes": "Risk a fall"})
            await self.call(session, "commit", {"campaign": "stdio-test", "action_id": begun["action_id"],
                "changes": [{"op": "consume_item", "id": "missing", "quantity": 1}]}, error=True)
            resumed = await self.call(session, "resume", {})
            self.assertEqual(resumed["guide"], load_guide())
            lean = await self.call(session, "resume", {"include_guide": False})
            self.assertNotIn("guide", lean)
            self.assertEqual(lean, {key: value for key, value in resumed.items() if key != "guide"})
            pending = resumed["pending"]
            self.assertEqual(pending["action_id"], begun["action_id"])
            await self.call(session, "cancel", {"campaign": "stdio-test", "action_id": begun["action_id"], "reason": "Player stops."})
            outcome = await self.call(session, "act", {"campaign": "stdio-test", "expected_revision": 2,
                "request_id": "wait", "changes": [], "beat": "The courier waits for daylight."})
            self.assertEqual(outcome["current_revision"], 3)


if __name__ == "__main__":
    unittest.main()
