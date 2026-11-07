"""Actionable contract errors without changing accepted input or saved state."""
import copy
from pathlib import Path
import tempfile
import unittest

from voice_rpg.contracts import GameError, OPERATION_SCHEMA, validate, validate_value
from voice_rpg.engine import Engine


def action(operation):
    return {"campaign": "frontier", "expected_revision": 0,
            "request_id": "handover", "changes": [operation]}


class ContractDiagnosticsTests(unittest.TestCase):
    def invalid(self, command, args):
        original = copy.deepcopy(args)
        with self.assertRaises(GameError) as caught:
            validate(command, args)
        self.assertEqual(caught.exception.code, "invalid_input")
        self.assertEqual(args, original)
        return caught.exception

    def test_unknown_or_missing_operation_lists_canonical_choices(self):
        expected = [option["properties"]["op"]["const"] for option in OPERATION_SCHEMA["oneOf"]]
        for operation in ({"op": "transfer", "id": "letter", "holder": "clerk"},
                          {"id": "letter"}, {"op": None}, {"op": {"name": "rest"}}):
            with self.subTest(operation=operation):
                error = self.invalid("act", action(operation))
                self.assertEqual(error.details["path"], "$.changes[0].op")
                self.assertEqual(error.details["allowed_values"], expected)
                self.assertIn("transfer_item", error.message)

    def test_selected_transfer_reports_all_missing_and_unknown_fields(self):
        error = self.invalid("act", action({"op": "transfer_item", "id": "letter", "holder": "clerk"}))
        self.assertEqual(error.details["path"], "$.changes[0].to")
        self.assertEqual(error.details["missing_fields"], ["to", "quantity"])
        self.assertEqual(error.details["unknown_fields"], ["holder"])
        self.assertIn("to", error.details["allowed_fields"])
        self.assertNotIn("holder", error.details["allowed_fields"])
        self.assertIn("to, quantity", error.message)
        self.assertIn("holder", error.message)
        for operation, path in [
            ({"op": "transfer_item", "id": "letter", "to": "clerk"}, "$.changes[0].quantity"),
            ({"op": "transfer_item", "id": "letter", "to": "clerk", "quantity": 1, "holder": "pc"},
             "$.changes[0].holder"),
            ({"op": "transfer_item", "id": "letter", "to": "clerk", "quantity": 0}, "$.changes[0].quantity"),
        ]:
            with self.subTest(operation=operation):
                self.assertEqual(self.invalid("act", action(operation)).details["path"], path)

    def test_goal_operations_share_one_target_field(self):
        for op in ("resolve_goal", "abandon_goal", "advance"):
            extra = {"talent": "sail"} if op == "advance" else {}
            with self.subTest(op=op):
                validate("act", action({"op": op, "id": "rescue", **extra}))
                error = self.invalid("act", action({"op": op, "goal_id": "rescue", **extra}))
                self.assertEqual(error.details["missing_fields"], ["id"])
                self.assertEqual(error.details["unknown_fields"], ["goal_id"])
                self.assertEqual(error.details["path"], "$.changes[0].id")

    def test_nested_record_kind_and_lookup_discriminator_report_choices(self):
        error = self.invalid("act", action({"op": "create_record", "record": {"id": "clerk", "kind": "person"}}))
        self.assertEqual(error.details["path"], "$.changes[0].record.kind")
        self.assertIn("npc", error.details["allowed_values"])
        self.assertNotIn("character", error.details["allowed_values"])
        error = self.invalid("lookup", {"campaign": "frontier", "selector": {"by": "inventory"}})
        self.assertEqual(error.details["path"], "$.selector.by")
        self.assertIn("holder", error.details["allowed_values"])
        self.assertEqual(error.details["allowed_values"].count("status"), 1)

    def test_selected_record_and_patch_errors_keep_nested_paths(self):
        error = self.invalid("act", action({"op": "create_record", "record": {"id": "clerk", "kind": "npc"}}))
        self.assertEqual(error.details["path"], "$.changes[0].record.name")
        error = self.invalid("act", action({"op": "patch_record", "id": "pc", "changes": {"health": 6}}))
        self.assertEqual(error.details["path"], "$.changes[0].changes.health")

    def test_repeated_status_discriminator_preserves_alternatives(self):
        for identity in ("action_id", "request_id"):
            validate("lookup", {"campaign": "frontier", "selector": {"by": "status", identity: "accepted"}})
        for selector, paths in [
            ({"by": "status"}, ["$.selector.action_id", "$.selector.request_id"]),
            ({"by": "status", "action_id": "a", "request_id": "r"},
             ["$.selector.request_id", "$.selector.action_id"]),
        ]:
            with self.subTest(selector=selector):
                error = self.invalid("lookup", {"campaign": "frontier", "selector": selector})
                self.assertEqual(error.details["path"], "$.selector")
                alternatives = error.details["alternatives"]
                self.assertEqual([entry["path"] for entry in alternatives], paths)
                self.assertEqual([entry["required_fields"] for entry in alternatives],
                                 [["by", "action_id"], ["by", "request_id"]])

    def test_union_match_count_and_sibling_constraints_remain_strict(self):
        option = {"type": "object", "properties": {"tag": {"const": "same"}}, "required": ["tag"]}
        value = {"tag": "same"}
        validate_value(value, {"anyOf": [option, option]})
        with self.assertRaises(GameError):
            validate_value(value, {"oneOf": [option, option]})
        with self.assertRaises(GameError):
            validate_value(value, {"anyOf": [option, option], "maxProperties": 0})
        # Optional constants cannot be used to exclude a branch: both accept {}.
        optional = {"type": "object", "properties": {"tag": {"const": "other"}}}
        validate_value({}, {"anyOf": [optional, optional]})
        with self.assertRaises(GameError):
            validate_value({}, {"oneOf": [optional, optional]})
        # Const equality keeps JSON booleans distinct from numbers.
        numeric = {"type": "object", "properties": {"tag": {"const": 1}}, "required": ["tag"]}
        validate_value({"tag": 1}, {"oneOf": [numeric]})
        with self.assertRaises(GameError):
            validate_value({"tag": True}, {"oneOf": [numeric]})

    def test_invalid_handover_never_mutates_or_consumes_request_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(Path(directory))
            engine.dispatch("create", {"campaign": "frontier", "request_id": "create", "title": "Frontier",
                "preferences": {}, "character": {"concept": "A traveler", "motivation": "Find a sibling",
                    "talents": ["notice", "talk", "climb"]},
                "possessions": [{"template": "item", "id": "letter", "name": "Letter"}],
                "opening": {"situation": "A clerk waits.", "present": ["clerk"],
                    "records": [{"id": "clerk", "kind": "npc", "name": "Clerk"}]}})
            before = engine.dispatch("resume", {})
            invalid = action({"op": "transfer", "id": "letter", "holder": "clerk"})
            with self.assertRaises(GameError) as caught:
                engine.dispatch("act", invalid)
            self.assertEqual(caught.exception.details["path"], "$.changes[0].op")
            self.assertEqual(engine.dispatch("resume", {}), before)
            accepted = action({"op": "transfer_item", "id": "letter", "to": "clerk", "quantity": 1})
            receipt = engine.dispatch("act", accepted)
            self.assertEqual(receipt["current_revision"], 1)
            self.assertTrue(engine.dispatch("act", accepted)["replayed"])
            saved = engine.dispatch("lookup", {"campaign": "frontier", "selector": {"by": "ids", "ids": ["letter"]}})
            self.assertEqual(saved["records"][0]["holder"], "clerk")
            self.assertEqual(saved["records"][0]["quantity"], 1)
            self.assertEqual(saved["revision"], 1)


if __name__ == "__main__":
    unittest.main()
