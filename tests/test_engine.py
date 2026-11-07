"""Acceptance tests for typed play, persistence and recovery through the public API."""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from voice_rpg.contracts import GameError
from voice_rpg.engine import Engine
from voice_rpg.guide import load_guide


class FixedDice:
    def __init__(self, *values):
        self.values = values or (4, 4)
        self.calls = 0

    def randint(self, lower, upper):
        value = self.values[self.calls % len(self.values)]
        self.calls += 1
        return value


class EngineIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name)
        self.rng = FixedDice()
        self.engine = Engine(self.data_dir, rng=self.rng)
        self.campaign = "river-road"
        self.sequence = 0

    def call(self, command, **args):
        args.setdefault("campaign", self.campaign)
        result = self.engine.dispatch(command, args)
        json.dumps(result, allow_nan=False)
        return result

    def create_args(self, **changes):
        args = {"campaign": self.campaign, "request_id": "create-river", "title": "The River Road",
                "preferences": {"genre": "fantasy", "tone": "hopeful mystery"},
                "character": {"name": "Mara", "concept": "A wandering bridge builder",
                              "motivation": "Bring the missing ferryman home", "talents": ["notice", "craft", "talk"]},
                "opening": {"situation": "A ferry drifts beside an empty landing.", "present": [], "records": []}}
        args.update(changes)
        return args

    def start(self, **changes):
        return self.engine.dispatch("create", self.create_args(**changes))

    def resume(self, **args):
        return self.call("resume", **args)

    def revision(self):
        return self.resume()["campaign"]["revision"]

    def lookup(self, selector, **args):
        return self.call("lookup", selector=selector, **args)

    def records(self, *ids):
        return self.lookup({"by": "ids", "ids": list(ids)})["records"]

    def record(self, identifier):
        records = self.records(identifier)
        self.assertEqual(len(records), 1, records)
        return records[0]

    def identity(self):
        self.sequence += 1
        return "request-%s" % self.sequence

    def act(self, changes, **args):
        args.setdefault("request_id", self.identity())
        args.setdefault("expected_revision", self.revision())
        return self.call("act", changes=changes, **args)

    def begin(self, intent="Find a safe route", stakes="A mistake could expose Mara.", **args):
        args.setdefault("request_id", self.identity())
        args.setdefault("expected_revision", self.revision())
        return self.call("begin", intent=intent, stakes=stakes, **args)

    def commit(self, action, changes, **args):
        return self.call("commit", action_id=action["action_id"], changes=changes, **args)

    def reject(self, command, **args):
        with self.assertRaises(GameError) as caught:
            self.call(command, **args)
        self.assertTrue(caught.exception.code)
        return caught.exception

    def reject_act(self, changes, **args):
        args.setdefault("expected_revision", self.revision())
        args.setdefault("request_id", self.identity())
        return self.reject("act", changes=changes, **args)

    @staticmethod
    def make(kind, identifier, **fields):
        return {"op": "create_record", "record": {"id": identifier, "kind": kind, **fields}}

    def race(self, calls):
        barrier = threading.Barrier(len(calls))
        def invoke(call):
            engine = Engine(self.data_dir, rng=FixedDice(3, 4))
            command, arguments = call
            barrier.wait(timeout=10)
            try:
                return engine.dispatch(command, {"campaign": self.campaign, **arguments})
            except GameError as error:
                return error
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            futures = [pool.submit(invoke, call) for call in calls]
            return [future.result(timeout=20) for future in futures]

    def test_creation_has_playable_talents_health_and_default_loadout(self):
        self.start()
        pc = self.record("pc")
        self.assertEqual(pc["health"], 6)
        self.assertEqual(pc["talents"], {"notice": 1, "craft": 1, "talk": 1})
        self.assertEqual(self.record("opening")["situation"], "A ferry drifts beside an empty landing.")
        self.assertEqual(self.record("signature_tool")["quantity"], 1)
        restorative = self.record("restorative")
        self.assertEqual((restorative["quantity"], restorative["holder"], restorative["use"]), (2, "pc", "heal"))
        self.assertEqual(self.revision(), 0)
        self.assertTrue((self.data_dir / "game.sqlite").is_file())
        self.assertFalse((self.data_dir / self.campaign).exists())

    def test_failed_creation_is_atomic_and_successful_creation_is_idempotent(self):
        arguments = self.create_args(opening={"situation": "An empty road", "present": ["missing"]})
        self.reject("create", **arguments)
        self.assertNotIn(self.campaign, json.dumps(self.engine.dispatch("resume", {})))
        arguments["opening"]["present"] = []
        result = self.engine.dispatch("create", arguments)
        retry = self.engine.dispatch("create", arguments)
        self.assertTrue(retry["replayed"])
        self.assertEqual(retry["action_id"], result["action_id"])
        self.assertEqual(self.revision(), 0)
        self.reject("create", **{**arguments, "title": "Changed title"})
        self.reject("create", **{**arguments, "request_id": "another-create"})

    def test_deprived_and_custom_loadouts_replace_defaults(self):
        self.start(possessions=[])
        self.assertEqual(self.lookup({"by": "holder", "holder": "pc"})["records"], [])
        self.engine.dispatch("create", self.create_args(campaign="custom", request_id="create-custom", possessions=[
            {"template": "gear", "id": "staff", "name": "Walking staff"},
            {"template": "restorative", "id": "tea", "name": "Restorative tea"},
            {"template": "item", "id": "letters", "name": "Letters", "quantity": 3}]))
        items = self.call("lookup", campaign="custom", selector={"by": "holder", "holder": "pc"})["records"]
        self.assertEqual({item["id"]: item["quantity"] for item in items}, {"staff": 1, "tea": 2, "letters": 3})

    def test_resume_selects_only_a_sole_campaign(self):
        self.assertIsNone(self.engine.dispatch("resume", {}).get("campaign"))
        self.start()
        self.assertEqual(self.engine.dispatch("resume", {})["campaign"]["id"], self.campaign)
        self.engine.dispatch("create", self.create_args(campaign="second-game", request_id="second-create"))
        picker = self.engine.dispatch("resume", {})
        self.assertIsNone(picker.get("campaign"))
        self.assertIn(self.campaign, json.dumps(picker))
        self.assertIn("second-game", json.dumps(picker))

    def test_resume_delivers_one_canonical_guide_by_default_without_changing_play(self):
        guide = load_guide()

        def check_delivery(selection):
            with closing(sqlite3.connect(self.engine.db_path)) as db:
                before = tuple(db.iterdump())
            lean = self.engine.dispatch("resume", dict(selection, include_guide=False))
            self.assertNotIn("guide", lean)
            for option in ({}, {"include_guide": True}, {"include_guide": False}):
                with self.subTest(selection=selection, option=option):
                    result = self.engine.dispatch("resume", dict(selection, **option))
                    if option.get("include_guide", True):
                        self.assertEqual(result.pop("guide"), guide)
                    else:
                        self.assertNotIn("guide", result)
                    self.assertEqual(result, lean)
            with closing(sqlite3.connect(self.engine.db_path)) as db:
                self.assertEqual(tuple(db.iterdump()), before)

        check_delivery({})  # Empty collection.
        self.assertNotIn("guide", self.start())
        pending = self.begin()
        self.assertNotIn("guide", pending)
        check_delivery({})  # Sole campaign, including its unchanged pending roll.
        self.engine.dispatch("create", self.create_args(campaign="second-game", request_id="second-create"))
        check_delivery({})  # Campaign picker.
        check_delivery({"campaign": self.campaign})
        self.assertEqual(self.resume(include_guide=False)["pending"]["action_id"], pending["action_id"])
        receipt = self.commit(pending, [self.make("scene", "arrival", situation="A new landing"),
                                       {"op": "set_scene", "id": "arrival"}])
        self.assertNotIn("guide", receipt)  # Scene-change receipts also use _context.

    def test_partial_patches_preserve_omitted_fields_and_only_allow_declared_nulls(self):
        self.start(opening={"situation": "At a bridge", "present": ["ferryman"], "records": [
            {"kind": "npc", "id": "ferryman", "name": "Oren", "text": "Carries a red scarf"},
            {"kind": "place", "id": "bank", "name": "River bank"}]})
        self.act([{"op": "patch_record", "id": "ferryman", "changes": {"text": "Offers a ride"}},
                  {"op": "patch_record", "id": "opening", "changes": {"place": "bank"}}])
        self.assertEqual(self.record("ferryman")["name"], "Oren")
        self.assertEqual(self.record("ferryman")["text"], "Offers a ride")
        self.act([{"op": "patch_record", "id": "opening", "changes": {"place": None}}])
        self.assertIsNone(self.record("opening")["place"])
        self.reject_act([{"op": "patch_record", "id": "ferryman", "changes": {"name": None}}])
        self.reject_act([{"op": "patch_record", "id": "ferryman", "changes": {"text": "First"}},
                         {"op": "patch_record", "id": "ferryman", "changes": {"text": "Second"}}])

    def test_patches_retirement_and_creation_cannot_bypass_protected_mechanics(self):
        self.start()
        self.act([self.make("goal", "promise", text="Return the boat", milestone=True)])
        before = self.resume()
        invalid = [
            {"op": "patch_record", "id": "pc", "changes": {"health": 99}},
            {"op": "patch_record", "id": "pc", "changes": {"talents": {"notice": 99}}},
            {"op": "patch_record", "id": "restorative", "changes": {"quantity": 99}},
            {"op": "patch_record", "id": "restorative", "changes": {"holder": "opening"}},
            {"op": "patch_record", "id": "promise", "changes": {"milestone": False}},
            {"op": "patch_record", "id": "promise", "changes": {"status": "resolved"}},
            {"op": "patch_record", "id": "promise", "changes": {"rewarded": True}},
            {"op": "retire_record", "id": "pc"}, {"op": "retire_record", "id": "restorative"},
            {"op": "retire_record", "id": "promise"},
            self.make("character", "another-pc", name="Another hero", health=6)]
        for operation in invalid:
            with self.subTest(operation=operation):
                self.reject_act([operation])
                self.assertEqual(self.resume(), before)

    def test_creations_are_staged_but_named_effects_follow_declared_order(self):
        self.start()
        self.act([{"op": "transfer_item", "id": "restorative", "to": "friend", "quantity": 1},
                  self.make("npc", "friend", name="A friendly traveler"),
                  {"op": "harm", "severity": "moderate"}, {"op": "rest"}, {"op": "harm", "severity": "minor"}])
        self.assertEqual(self.record("pc")["health"], 5)
        self.assertEqual(self.record("restorative")["quantity"], 1)
        self.assertEqual(len(self.lookup({"by": "holder", "holder": "friend"})["records"]), 1)
        before = self.resume()
        self.reject_act([self.make("npc", "duplicate", name="First"), self.make("npc", "duplicate", name="Second")])
        self.assertEqual(self.resume(), before)

    def test_retirement_requires_current_references_to_be_repaired_atomically(self):
        self.start(opening={"situation": "At a bridge", "present": ["ferryman"], "records": [
            {"kind": "npc", "id": "ferryman", "name": "Oren"}]})
        self.reject_act([{"op": "retire_record", "id": "opening"}])
        self.reject_act([{"op": "retire_record", "id": "ferryman"}])
        self.act([{"op": "retire_record", "id": "ferryman"},
                  {"op": "patch_record", "id": "opening", "changes": {"present": []}}])
        self.assertTrue(self.record("ferryman")["archived"])

    def test_harm_rest_and_restorative_use_are_bounded_and_atomic(self):
        self.start()
        self.act([{"op": "harm", "severity": "severe"}, {"op": "harm", "severity": "severe"}, {"op": "harm", "severity": "minor"}])
        self.assertEqual(self.record("pc")["health"], 0)
        arguments = {"expected_revision": self.revision(), "request_id": "use-once", "changes": [{"op": "use_item", "id": "restorative"}]}
        self.call("act", **arguments)
        self.call("act", **arguments)
        self.assertEqual(self.record("pc")["health"], 2)
        self.assertEqual(self.record("restorative")["quantity"], 1)
        self.act([{"op": "rest"}, {"op": "use_item", "id": "restorative"}])
        self.assertEqual(self.record("pc")["health"], 6)
        self.assertEqual(self.record("restorative")["quantity"], 0)
        self.assertNotIn("restorative", {item["id"] for item in self.lookup({"by": "holder", "holder": "pc"})["records"]})
        before = self.resume()
        self.reject_act([{"op": "harm", "severity": "minor"}, {"op": "use_item", "id": "restorative"}])
        self.assertEqual(self.resume(), before)

    def test_split_transfer_retry_preserves_identity_stock_and_ownership(self):
        self.start()
        self.act([self.make("npc", "friend", name="A traveler")])
        arguments = {"expected_revision": self.revision(), "request_id": "share-remedy",
                     "changes": [{"op": "transfer_item", "id": "restorative", "to": "friend", "quantity": 1}]}
        first = self.call("act", **arguments)
        retry = self.call("act", **arguments)
        held = self.lookup({"by": "holder", "holder": "friend"})["records"]
        self.assertEqual(len(held), 1)
        split = held[0]
        self.assertNotEqual(split["id"], "restorative")
        self.assertEqual((split["quantity"], split["use"]), (1, "heal"))
        self.assertIn(split["id"], json.dumps(first))
        self.assertIn(split["id"], json.dumps(retry))
        self.assertEqual(self.record("restorative")["quantity"], 1)
        self.reject_act([{"op": "use_item", "id": split["id"]}])
        self.reject_act([{"op": "consume_item", "id": split["id"], "quantity": 1}])
        self.reject_act([{"op": "transfer_item", "id": "restorative", "to": "friend", "quantity": 2}])
        self.act([{"op": "transfer_item", "id": split["id"], "to": "pc", "quantity": 1, "expected_holder": "friend"}])
        self.assertEqual(self.record(split["id"])["holder"], "pc")

    def test_invalid_container_reference_is_actionable_atomic_and_retriable(self):
        self.start()
        self.act([self.make("item", "vehicle", name="Vehicle"),
                  self.make("place", "cabin", name="Cabin")])
        before = self.resume()
        request = {"expected_revision": self.revision(), "request_id": "stow-supplies",
            "changes": [{"op": "harm", "severity": "minor"},
                        {"op": "transfer_item", "id": "restorative", "to": "vehicle", "quantity": 2}]}
        error = self.reject("act", **request)
        self.assertEqual(error.code, "invalid_reference")
        self.assertEqual(error.details, {"id": "vehicle", "actual_kind": "item",
            "expected_kinds": ["character", "npc", "place", "scene"]})
        self.assertEqual(self.resume(), before)
        request["changes"][1]["to"] = "cabin"
        first = self.call("act", **request)
        self.assertEqual(first["current_revision"], before["campaign"]["revision"] + 1)
        self.assertTrue(self.call("act", **request)["replayed"])
        self.assertEqual(self.record("pc")["health"], 5)
        self.assertEqual((self.record("restorative")["holder"], self.record("restorative")["quantity"]),
                         ("cabin", 2))
        self.engine.restore(self.engine.backup())
        self.assertTrue(self.call("act", **request)["replayed"])
        self.assertEqual(self.record("pc")["health"], 5)
        self.assertEqual(self.record("restorative")["holder"], "cabin")

    def test_growth_rewards_are_preexisting_once_only_and_capped(self):
        self.start()
        self.act([self.make("goal", "rescue", text="Rescue the ferryman", milestone=True)])
        self.act([{"op": "resolve_goal", "id": "rescue"}])
        self.assertFalse(self.record("rescue")["rewarded"])
        self.assertIn("rescue", json.dumps(self.resume()))
        advance = {"expected_revision": self.revision(), "request_id": "claim-rescue",
                   "changes": [{"op": "advance", "id": "rescue", "talent": "notice"}]}
        self.call("act", **advance)
        self.assertEqual(self.record("pc")["talents"]["notice"], 2)
        self.assertTrue(self.record("rescue")["rewarded"])
        self.engine.restore(self.engine.backup())
        self.assertTrue(self.call("act", **advance)["replayed"])
        self.assertEqual(self.record("pc")["talents"]["notice"], 2)
        self.assertTrue(self.record("rescue")["rewarded"])
        self.reject_act([{"op": "advance", "id": "rescue", "talent": "craft"}])
        self.reject_act([self.make("goal", "instant", text="An instant milestone", milestone=True),
                         {"op": "resolve_goal", "id": "instant"}, {"op": "advance", "id": "instant", "talent": "sail"}])
        self.act([self.make("goal", "bridge", text="Repair the bridge", milestone=True)])
        self.reject_act([{"op": "resolve_goal", "id": "bridge"}, {"op": "advance", "id": "bridge", "talent": "notice"}])
        self.assertEqual(self.record("bridge")["status"], "active")
        self.act([{"op": "resolve_goal", "id": "bridge"}, {"op": "advance", "id": "bridge", "talent": "sail"}])
        self.assertEqual(self.record("pc")["talents"]["sail"], 1)
        self.act([self.make("goal", "abandoned", text="Leave the road", milestone=True), {"op": "abandon_goal", "id": "abandoned"}])
        self.reject_act([{"op": "advance", "id": "abandoned", "talent": "craft"}])

    def test_begin_cancel_both_advance_revision_and_reject_delayed_old_decisions(self):
        self.start()
        revision = self.revision()
        action = self.begin(talent="notice")
        self.assertEqual(self.revision(), revision + 1)
        self.call("cancel", action_id=action["action_id"], reason="The player decides to wait.")
        self.assertEqual(self.revision(), revision + 2)
        self.reject("act", expected_revision=revision, request_id="late-request", changes=[], beat="A stale decision arrives.")
        self.assertEqual(self.revision(), revision + 2)

    def test_failed_commit_is_atomic_and_saved_roll_survives_restart(self):
        self.start()
        action = self.begin(talent="notice", difficulty="hard")
        before = self.resume()
        draws = self.rng.calls
        self.reject("commit", action_id=action["action_id"], changes=[
            self.make("fact", "promise", text="Return the boat", subjects=["pc"]),
            {"op": "consume_item", "id": "restorative", "quantity": 3}])
        self.assertEqual(self.resume(), before)
        self.assertEqual(self.rng.calls, draws)
        never_roll = FixedDice(1, 1)
        self.engine = Engine(self.data_dir, rng=never_roll)
        self.assertEqual(self.resume()["pending"], before["pending"])
        self.commit(action, [{"op": "harm", "severity": "minor"}], beat="Mara finds the route at a cost.")
        self.assertEqual(never_roll.calls, 0)
        self.assertEqual(self.record("pc")["health"], 5)

    def test_replay_reports_current_status_without_replaying_old_changes(self):
        created = self.start()
        arguments = {"expected_revision": self.revision(), "request_id": "saved-attempt",
                     "intent": "Find the hidden crossing", "stakes": "Mara could lose supplies.", "talent": "notice"}
        begun = self.call("begin", **arguments)
        retried_pending = self.call("begin", **arguments)
        self.assertEqual(retried_pending["action_id"], begun["action_id"])
        self.assertEqual(retried_pending["check"], begun["check"])
        self.assertEqual(retried_pending["status"], "pending")
        self.assertEqual(self.rng.calls, 2)
        committed = self.commit(begun, [{"op": "harm", "severity": "minor"}], beat="Mara finds the crossing.")
        for replay in [self.call("begin", **arguments), self.engine.dispatch("create", self.create_args())]:
            self.assertTrue(replay["replayed"])
            self.assertTrue(replay["refresh_required"])
            self.assertEqual(replay["current_revision"], self.revision())
            self.assertNotIn("changes", replay)
            self.assertNotIn("context", replay)
        self.assertEqual(self.call("begin", **arguments)["status"], "completed")
        self.assertLess(created["accepted_revision"], committed["accepted_revision"])

    def test_request_ids_are_shared_across_mutating_commands(self):
        self.start()
        self.act([], request_id="shared-identity", beat="The ferryman nods.")
        before = self.resume()
        self.reject("begin", expected_revision=self.revision(), request_id="shared-identity", intent="Cross the river", stakes="Lose the way")
        self.assertEqual(self.resume(), before)
        self.reject("act", expected_revision=self.revision(), request_id="shared-identity", changes=[], beat="A different nod.")

    def test_concurrent_begin_and_act_have_one_winner(self):
        self.start()
        revision = self.revision()
        results = self.race([
            ("begin", {"expected_revision": revision, "request_id": "uncertain", "intent": "Find a crossing", "stakes": "Become lost"}),
            ("act", {"expected_revision": revision, "request_id": "known", "changes": [], "beat": "Mara stays ashore."})])
        self.assertEqual(sum(not isinstance(result, GameError) for result in results), 1, results)
        self.assertEqual(self.revision(), revision + 1)

    def test_concurrent_commit_and_cancel_have_one_closing_disposition(self):
        self.start()
        action = self.begin()
        revision = self.revision()
        results = self.race([
            ("commit", {"action_id": action["action_id"], "changes": [{"op": "harm", "severity": "minor"}]}),
            ("cancel", {"action_id": action["action_id"], "reason": "The player abandons the attempt."})])
        self.assertEqual(sum(not isinstance(result, GameError) for result in results), 1, results)
        self.assertEqual(self.revision(), revision + 1)
        self.assertIsNone(self.resume()["pending"])
        self.assertIn(self.record("pc")["health"], [5, 6])

    def test_concurrent_exact_commit_retries_apply_effect_once(self):
        self.start()
        action = self.begin()
        revision = self.revision()
        arguments = {"action_id": action["action_id"], "changes": [{"op": "harm", "severity": "moderate"}]}
        results = self.race([("commit", arguments), ("commit", copy.deepcopy(arguments))])
        self.assertFalse(any(isinstance(result, GameError) for result in results), results)
        self.assertEqual(self.revision(), revision + 1)
        self.assertEqual(self.record("pc")["health"], 4)
        self.assertEqual({result["action_id"] for result in results}, {action["action_id"]})

    def test_pending_correction_preserves_raw_dice_and_recomputes_modifiers(self):
        self.start()
        action = self.begin(talent="notice", difficulty="hard")
        original = self.resume()["pending"]
        draws = self.rng.calls
        revision = self.revision()
        self.call("correct", expected_revision=self.revision(), request_id="fix-intention", target_action_id=action["action_id"],
                  reason="The player said inspect the bridge.", replacement_check={"intent": "Inspect the bridge",
                  "stakes": "A weak plank may give way.", "talent": "craft", "difficulty": "easy"})
        replacement = self.resume()["pending"]
        self.assertEqual(self.revision(), revision + 1)
        self.assertNotEqual(replacement["action_id"], action["action_id"])
        self.assertEqual(replacement["check"]["dice"], original["check"]["dice"])
        self.assertEqual(replacement["check"]["total"], original["check"]["total"] + 2)
        self.assertEqual(self.rng.calls, draws)
        self.reject("commit", action_id=action["action_id"], changes=[], beat="Old attempt")
        self.commit(replacement, [], beat="Mara understands the bridge.")

    def test_completed_correction_restores_world_and_old_commit_cannot_reapply(self):
        self.start()
        action = self.begin(intent="Drink the strange remedy", talent="notice")
        closing = {"action_id": action["action_id"], "changes": [
            {"op": "harm", "severity": "moderate"}, {"op": "consume_item", "id": "restorative", "quantity": 1},
            self.make("fact", "empty-cup", text="The cup is empty", subjects=["pc"])], "beat": "Mara drank the remedy."}
        self.call("commit", **closing)
        draws = self.rng.calls
        self.call("correct", expected_revision=self.revision(), request_id="inspect-instead", target_action_id=action["action_id"],
                  reason="Mara only inspected the remedy.", replacement_check={"intent": "Inspect the remedy",
                  "stakes": "Its nature may remain unclear.", "talent": "notice"})
        self.assertEqual(self.record("pc")["health"], 6)
        self.assertEqual(self.record("restorative")["quantity"], 2)
        self.assertEqual(self.reject("lookup", selector={"by": "ids", "ids": ["empty-cup"]}).code, "not_found")
        self.assertEqual(self.rng.calls, draws)
        before = self.resume()
        replay = self.call("commit", **closing)
        self.assertEqual(replay["status"], "corrected")
        self.assertTrue(replay["refresh_required"])
        self.assertNotIn("changes", replay)
        self.assertEqual(self.resume(), before)
        self.assertNotIn("Mara drank the remedy.", json.dumps(self.resume()))

    def test_correction_boundary_and_unrelated_pending_attempt_block_older_undo(self):
        self.start()
        first = self.act([], beat="Mara promises to listen.")
        second = self.act([], beat="Mara steps onto the bridge.")
        self.reject("correct", expected_revision=self.revision(), request_id="old-correction", target_action_id=first["action_id"], reason="Too old")
        self.call("correct", expected_revision=self.revision(), request_id="latest-correction", target_action_id=second["action_id"], reason="Mara remained ashore.")
        self.reject("correct", expected_revision=self.revision(), request_id="skip-boundary", target_action_id=first["action_id"], reason="Skip correction")
        later = self.act([], beat="Mara examines the rope.")
        self.begin()
        self.reject("correct", expected_revision=self.revision(), request_id="pending-block", target_action_id=later["action_id"], reason="Unrelated attempt")

    def test_reverse_subject_facts_and_nearby_items_survive_travel_and_restart(self):
        self.start(opening={"situation": "At the landing", "present": ["ferryman"], "records": [
            {"kind": "npc", "id": "ferryman", "name": "Oren"},
            {"kind": "fact", "id": "secret", "text": "Oren hid the map", "subjects": ["ferryman"], "visibility": "gm"},
            {"kind": "fact", "id": "claim", "text": "Oren says the map was stolen", "subjects": ["ferryman"], "truth": "claim", "source": "ferryman"}]})
        self.assertIn("Oren hid the map", json.dumps(self.resume()))
        self.act([{"op": "transfer_item", "id": "signature_tool", "to": "opening", "quantity": 1},
                  self.make("scene", "forest", situation="The quiet woods", present=[]), {"op": "set_scene", "id": "forest"}])
        self.act([{"op": "set_scene", "id": "opening"}])
        self.engine = Engine(self.data_dir, rng=FixedDice())
        resumed = json.dumps(self.resume())
        self.assertIn("Oren hid the map", resumed)
        self.assertIn("Oren says the map was stolen", resumed)
        self.assertIn("signature_tool", resumed)
        self.assertEqual(self.record("signature_tool")["holder"], "opening")
        for response in [self.resume(audience="player"), self.lookup({"by": "subjects", "subjects": ["ferryman"]}, audience="player"),
                         self.lookup({"by": "search", "query": "hid the map"}, audience="player")]:
            self.assertNotIn("Oren hid the map", json.dumps(response))
            self.assertNotIn('"secret"', json.dumps(response))

    def test_pagination_exposes_omissions_and_rejects_stale_cursor(self):
        self.start()
        for start, end in [(0, 100), (100, 125)]:
            self.act([self.make("fact", "fact-%03d" % index, text="Clue %s" % index, subjects=["pc"])
                      for index in range(start, end)])
        omitted = self.resume()["omitted_facts"]
        self.assertEqual(omitted["count"], 105)
        self.assertEqual(len(omitted["ids"]), 100)
        self.assertIn("selector", omitted)
        self.assertIn("cursor", omitted)
        selector = {"by": "subjects", "subjects": ["pc"], "limit": 10}
        first = self.lookup(selector)
        self.assertEqual(len(first["records"]), 10)
        self.assertTrue(first["cursor"])
        second = self.lookup(selector, cursor=first["cursor"])
        self.assertFalse({r["id"] for r in first["records"]} & {r["id"] for r in second["records"]})
        self.assertEqual(self.lookup(selector), first)
        self.act([], beat="The light changes.")
        self.reject("lookup", selector=selector, cursor=first["cursor"])

    def test_essential_context_pages_are_explicit_and_complete(self):
        self.start()
        changes = []
        for index in range(25):
            changes.extend([
                self.make("npc", "npc-%02d" % index, name="Traveler %s" % index),
                self.make("goal", "goal-%02d" % index, text="Help traveler %s" % index),
                self.make("item", "item-%02d" % index, name="Supply %s" % index)])
        changes.append({"op": "patch_record", "id": "opening",
                        "changes": {"present": ["npc-%02d" % index for index in range(25)]}})
        self.act(changes)
        packet = self.resume()
        self.assertTrue(packet["context_incomplete"])
        for section, total in [("inventory", 27), ("present", 25), ("goals", 25)]:
            with self.subTest(section=section):
                seen = {record["id"] for record in packet[section]}
                self.assertEqual(len(seen), 20)
                continuation = packet["continuations"][section]
                cursor = continuation["cursor"]
                pages = 0
                while cursor:
                    page = self.lookup(continuation["selector"], cursor=cursor)
                    ids = {record["id"] for record in page["records"]}
                    self.assertFalse(seen & ids)
                    seen.update(ids)
                    cursor = page.get("cursor")
                    pages += 1
                    self.assertLess(pages, 5)
                self.assertEqual(len(seen), total)
        self.act([], beat="A new traveler speaks.")
        continuation = packet["continuations"]["inventory"]
        self.reject("lookup", selector=continuation["selector"], cursor=continuation["cursor"])

    def test_only_last_three_uncorrected_beats_appear_while_promises_persist(self):
        self.start()
        self.act([self.make("goal", "promise", text="Return the borrowed boat", subjects=["pc"])])
        for index in range(5):
            self.act([], beat="Transient exchange number %s." % index)
        text = json.dumps(self.resume())
        for index in range(2):
            self.assertNotIn("Transient exchange number %s." % index, text)
        for index in range(2, 5):
            self.assertIn("Transient exchange number %s." % index, text)
        self.assertIn("Return the borrowed boat", text)

    def test_collection_backup_restores_pending_roll_and_request_identity(self):
        self.start()
        arguments = {"expected_revision": self.revision(), "request_id": "before-backup", "intent": "Inspect the crossing", "stakes": "Lose footing", "talent": "notice"}
        action = self.call("begin", **arguments)
        pending = self.resume()["pending"]
        backup = Path(self.engine.backup())
        self.commit(action, [{"op": "harm", "severity": "moderate"}])
        self.engine.restore(backup)
        self.engine = Engine(self.data_dir, rng=FixedDice(1, 1))
        self.assertEqual(self.resume()["pending"], pending)
        self.assertEqual(self.record("pc")["health"], 6)
        replay = self.call("begin", **arguments)
        self.assertEqual(replay["action_id"], action["action_id"])
        self.assertEqual(replay["check"]["dice"], action["check"]["dice"])
        self.commit(action, [{"op": "harm", "severity": "minor"}])
        self.assertEqual(self.record("pc")["health"], 5)

    def test_restore_after_crashed_writer_does_not_replay_old_sidecars(self):
        child = r'''
import os, sqlite3, sys
from voice_rpg.engine import Engine
directory, mode = sys.argv[1:]
engine = Engine(directory)
keeper = sqlite3.connect(engine.db_path)
keeper.execute("PRAGMA journal_mode=" + mode)
keeper.execute("PRAGMA wal_autocheckpoint=0")
if mode == "WAL":
    keeper.execute("BEGIN")
    keeper.execute("SELECT count(*) FROM campaigns").fetchone()
engine.dispatch("act", {"campaign": "river-road", "request_id": "harm",
    "expected_revision": 0, "changes": [{"op": "harm", "severity": "moderate"}]})
if mode == "DELETE":
    original = engine._request
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(0)
    engine._request = crash
    engine.dispatch("act", {"campaign": "river-road", "request_id": "interrupted",
        "expected_revision": 1, "changes": [
            {"op": "patch_record", "id": "item" + str(index),
             "changes": {"text": "😁" * 4000}} for index in range(100)]})
os._exit(0)
'''
        for mode, fail_replace in (("DELETE", False), ("WAL", False), ("DELETE", True), ("WAL", True)):
            with self.subTest(mode=mode, fail_replace=fail_replace), tempfile.TemporaryDirectory() as directory:
                engine = Engine(directory)
                args = self.create_args(opening={"situation": "At the landing.", "records": [
                    {"id": "item" + str(index), "kind": "item", "name": "Story item",
                     "text": "😀" * 4000} for index in range(100)]})
                engine.dispatch("create", args)
                expected = engine.dispatch("resume", {"campaign": self.campaign})
                backup = engine.backup()
                subprocess.run([sys.executable, "-c", child, directory, mode],
                    cwd=Path(__file__).resolve().parents[1], check=True, timeout=20)
                sidecars = [Path(str(engine.db_path) + suffix) for suffix in ("-journal", "-wal", "-shm")]
                evidence = {path: path.read_bytes() for path in sidecars if path.exists()}
                self.assertTrue(evidence)
                if mode == "DELETE":
                    self.assertEqual(evidence[sidecars[0]][:8].hex(), "d9d505f920a163d7")
                else:
                    self.assertIn(sidecars[1], evidence)
                    self.assertIn(sidecars[2], evidence)
                invalid = Path(directory) / "invalid.sqlite"
                invalid.write_bytes(b"not a SQLite backup")
                with self.assertRaises(GameError):
                    engine.restore(invalid)
                self.assertEqual({path: path.read_bytes() for path in evidence}, evidence)
                if fail_replace:
                    with patch("voice_rpg.engine.os.replace", side_effect=OSError("replacement failed")):
                        with self.assertRaises(OSError):
                            engine.restore(backup)
                    surviving = Engine(directory).dispatch("resume", {"campaign": self.campaign})
                    self.assertEqual(surviving["campaign"]["revision"], 1)
                    self.assertEqual(surviving["character"]["health"], 4)
                    item = engine.dispatch("lookup", {"campaign": self.campaign,
                        "selector": {"by": "ids", "ids": ["item0"]}})["records"][0]
                    self.assertEqual(item["text"], "😀" * 4000)
                    continue
                engine.restore(backup)
                self.assertFalse(any(path.exists() for path in sidecars))
                reopened = Engine(directory)
                self.assertEqual(reopened.dispatch("resume", {"campaign": self.campaign}), expected)
                with closing(sqlite3.connect(reopened.db_path)) as db:
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_restore_stops_before_replacing_an_unrecoverable_destination(self):
        self.start()
        expected = self.resume()
        backup = self.engine.backup()
        self.engine.db_path.write_bytes(b"not a SQLite database")
        journal = Path(str(self.engine.db_path) + "-wal")
        journal.write_bytes(b"damaged recovery file")
        with self.assertRaises(GameError) as caught:
            self.engine.restore(backup)
        self.assertEqual(caught.exception.code, "restore_failed")
        self.assertEqual(self.engine.db_path.read_bytes(), b"not a SQLite database")
        # A damaged collection can be restored after its file set is moved aside.
        displaced = self.data_dir / "damaged"
        displaced.mkdir()
        self.engine.db_path.rename(displaced / "game.sqlite")
        if journal.exists():
            journal.rename(displaced / "game.sqlite-wal")
        self.engine.restore(backup)
        self.assertEqual(self.resume(), expected)

    def test_restore_rejects_corrupt_metadata_without_replacing_live_state(self):
        self.start()
        source = Path(self.engine.backup())
        corrupt = self.data_dir / "corrupt-backup.sqlite"
        corrupt.write_bytes(source.read_bytes())
        db = sqlite3.connect(str(corrupt))
        try:
            metadata = json.loads(db.execute("SELECT data FROM campaigns WHERE id=?", (self.campaign,)).fetchone()[0])
            metadata["revision"] = -1
            db.execute("UPDATE campaigns SET data=? WHERE id=?", (json.dumps(metadata), self.campaign))
            db.commit()
        finally:
            db.close()
        before = self.resume()
        with self.assertRaises(GameError):
            self.engine.restore(corrupt)
        self.assertEqual(self.resume(), before)

    def test_restore_rejects_broken_pending_history_and_retry_identity_atomically(self):
        self.start()
        arguments = {"campaign": self.campaign, "request_id": "saved-attempt", "expected_revision": 0,
                     "intent": "Cross the bridge", "stakes": "Fall into the river", "talent": "notice"}
        action = self.engine.dispatch("begin", arguments)
        source = Path(self.engine.backup()).read_bytes()
        before = self.resume()
        live_bytes = self.engine.db_path.read_bytes()
        variants = ("malformed_json", "missing_check", "wrong_total", "wrong_revision", "wrong_status",
                    "bad_retry_hash", "missing_retry", "wrong_retry_revision", "orphan_replacement")
        for variant in variants:
            with self.subTest(variant=variant):
                corrupt = self.data_dir / "invalid-history.sqlite"
                corrupt.write_bytes(source)
                with closing(sqlite3.connect(str(corrupt))) as db, db:
                    data = json.loads(db.execute("SELECT data FROM actions WHERE id=?", (action["action_id"],)).fetchone()[0])
                    if variant == "malformed_json":
                        db.execute("UPDATE actions SET data='{' WHERE id=?", (action["action_id"],))
                    elif variant in ("missing_check", "wrong_total", "orphan_replacement"):
                        if variant == "missing_check":
                            data.pop("check")
                        elif variant == "wrong_total":
                            data["check"]["total"] += 1
                        else:
                            data.update(replaces="missing-action", correction_action_id="missing-correction")
                        db.execute("UPDATE actions SET data=? WHERE id=?", (json.dumps(data), action["action_id"]))
                    elif variant == "wrong_revision":
                        db.execute("UPDATE actions SET created_revision=999 WHERE id=?", (action["action_id"],))
                    elif variant == "wrong_status":
                        db.execute("UPDATE actions SET status='completed' WHERE id=?", (action["action_id"],))
                    elif variant == "bad_retry_hash":
                        db.execute("UPDATE requests SET payload_hash='invalid' WHERE id='saved-attempt'")
                    elif variant == "missing_retry":
                        db.execute("DELETE FROM requests WHERE id='saved-attempt'")
                    else:
                        db.execute("UPDATE requests SET accepted_revision=0 WHERE id='saved-attempt'")
                with self.assertRaises(GameError) as caught:
                    self.engine.restore(corrupt)
                self.assertEqual(caught.exception.code, "invalid_backup")
                self.assertEqual(self.engine.db_path.read_bytes(), live_bytes)
                self.assertEqual(self.resume(), before)
                self.assertEqual(self.engine.dispatch("begin", arguments)["check"], action["check"])
        self.commit(action, [], beat="Mara reaches the other side.")

    def test_restore_rejects_damaged_correction_links_images_and_reused_roll(self):
        self.start()
        attempt = self.begin(talent="notice")
        self.commit(attempt, [{"op": "harm", "severity": "moderate"}])
        correction = self.call("correct", expected_revision=self.revision(), request_id="repair",
            target_action_id=attempt["action_id"], reason="The player meant the other crossing.",
            replacement_check={"intent": "Take the other crossing", "stakes": "Slip", "talent": "notice"})
        source = Path(self.engine.backup()).read_bytes()
        before = self.resume()
        for variant in ("missing_target", "invalid_before_image", "changed_dice", "missing_correction"):
            with self.subTest(variant=variant):
                corrupt = self.data_dir / "invalid-correction.sqlite"
                corrupt.write_bytes(source)
                with closing(sqlite3.connect(str(corrupt))) as db, db:
                    if variant == "missing_correction":
                        db.execute("DELETE FROM requests WHERE action_id=?", (correction["action_id"],))
                        db.execute("DELETE FROM actions WHERE id=?", (correction["action_id"],))
                    else:
                        identity = correction["replacement_action_id"] if variant == "changed_dice" else correction["action_id"]
                        data = json.loads(db.execute("SELECT data FROM actions WHERE id=?", (identity,)).fetchone()[0])
                        if variant == "missing_target":
                            data["target_action_id"] = "missing-action"
                        elif variant == "invalid_before_image":
                            data["before"]["records"]["pc"]["health"] = "healthy"
                        else:
                            data["check"].update(dice=[3, 5])  # same total, a different saved roll
                        db.execute("UPDATE actions SET data=? WHERE id=?", (json.dumps(data), identity))
                with self.assertRaises(GameError) as caught:
                    self.engine.restore(corrupt)
                self.assertEqual(caught.exception.code, "invalid_backup")
                self.assertEqual(self.resume(), before)

    def test_restore_preserves_correction_chains_cancelled_rolls_and_historical_receipts(self):
        self.start()
        self.act([self.make("npc", "friend", name="A traveler")])
        attempt = self.begin(talent="notice")
        closing = {"campaign": self.campaign, "action_id": attempt["action_id"], "changes": [
            {"op": "transfer_item", "id": "restorative", "to": "friend", "quantity": 1},
            {"op": "harm", "severity": "moderate"}]}
        self.engine.dispatch("commit", closing)
        repaired = self.call("correct", expected_revision=self.revision(), request_id="first-repair",
            target_action_id=attempt["action_id"], reason="Correct the approach.",
            replacement_check={"intent": "Try the ridge", "stakes": "Slip", "difficulty": "hard"})
        repaired_again = self.call("correct", expected_revision=self.revision(), request_id="second-repair",
            target_action_id=repaired["replacement_action_id"], reason="Clarify the ridge route.",
            replacement_check={"intent": "Use the sheltered ridge", "stakes": "Slip", "talent": "notice"})
        self.call("cancel", action_id=repaired_again["replacement_action_id"], reason="The player waits.")
        known = self.act([{"op": "harm", "severity": "minor"}])
        self.call("correct", expected_revision=self.revision(), request_id="known-repair",
                  target_action_id=known["action_id"], reason="The injury was a mistake.")
        current = self.begin(talent="craft")
        expected = self.resume()
        backup = Path(self.engine.backup())
        self.commit(current, [{"op": "harm", "severity": "severe"}])
        self.engine.restore(backup)
        self.assertEqual(self.resume(), expected)
        replay = self.engine.dispatch("commit", closing)
        self.assertTrue(replay["replayed"])
        self.assertTrue(replay["refresh_required"])
        self.assertEqual(replay["status"], "corrected")
        self.assertEqual(self.record("restorative")["quantity"], 2)
        self.assertEqual(self.record("pc")["health"], 6)
        self.assertEqual(self.resume()["pending"]["check"], current["check"])
        self.commit(current, [{"op": "harm", "severity": "minor"}])
        completed = self.resume()
        self.engine.restore(self.engine.backup())
        self.assertEqual(self.resume(), completed)

    def test_backup_failure_reports_acceptance_and_exact_retry_does_not_repeat_effect(self):
        self.start()
        arguments = {"campaign": self.campaign, "expected_revision": 0, "request_id": "injury",
                     "changes": [{"op": "harm", "severity": "moderate"}]}
        with patch.object(self.engine, "backup", side_effect=OSError("Synthetic backup failure")) as backup:
            result = self.engine.dispatch("act", arguments)
            replay = self.engine.dispatch("act", arguments)
        self.assertEqual(result["warning"]["code"], "backup_failed")
        self.assertTrue(result["warning"]["accepted"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(backup.call_count, 1)
        self.assertEqual(self.record("pc")["health"], 4)
        self.assertEqual(self.revision(), 1)

    def test_scene_change_refreshes_destination_without_repeating_sheet_or_losing_effects(self):
        self.start()
        result = self.act([
            self.make("scene", "refuge", situation="A bell rings in the abandoned refuge."),
            self.make("fact", "bell", text="The bell rope is moving by itself.", subjects=["refuge"]),
            {"op": "set_scene", "id": "refuge"},
            {"op": "harm", "severity": "minor"},
            {"op": "consume_item", "id": "restorative", "quantity": 1},
        ])
        packet = result["context"]
        self.assertEqual(packet["scene"]["id"], "refuge")
        self.assertEqual(packet["facts"][0]["id"], "bell")
        for repeated in ("rules", "character", "inventory", "campaign", "recent_beats"):
            self.assertNotIn(repeated, packet)
        changed = {record["id"]: record for record in result["changes"]}
        self.assertEqual(changed["pc"]["health"], 5)
        self.assertEqual(changed["restorative"]["quantity"], 1)
        self.assertNotIn("refuge", changed)
        self.assertNotIn("bell", changed)

    def test_scenario_conversation_exploration_conflict_recovery_growth_repair_and_resume(self):
        self.start(opening={"situation": "The landing is deserted", "present": ["guide"], "records": [
            {"kind": "npc", "id": "guide", "name": "Edda"},
            {"kind": "goal", "id": "rescue", "text": "Bring the ferryman home", "subjects": ["pc"], "milestone": True}]})
        self.act([self.make("fact", "offer", text="Edda offers passage for help", subjects=["guide"], truth="claim", source="guide")])
        exploration = self.begin(intent="Use bridge-building skill to find a submerged ledge", talent="craft")
        self.commit(exploration, [self.make("fact", "ledge", text="A submerged ledge reaches the island", subjects=["opening"])], beat="Mara finds a surprising route.")
        conflict = self.begin(intent="Distract the bandits so the ferryman can escape", talent="talk", difficulty="hard")
        self.commit(conflict, [{"op": "harm", "severity": "severe"}], beat="The ferryman escapes while Mara is injured.")
        retreat = self.begin(intent="Retreat along the submerged ledge", talent="notice")
        self.commit(retreat, [self.make("scene", "shelter", situation="Safe beneath an old bridge", present=["guide"]), {"op": "set_scene", "id": "shelter"}])
        self.act([{"op": "use_item", "id": "restorative"}])
        self.assertEqual(self.record("pc")["health"], 5)
        self.act([{"op": "rest"}, {"op": "resolve_goal", "id": "rescue"}, {"op": "advance", "id": "rescue", "talent": "craft"}])
        mistaken = self.begin(intent="Drink the remaining remedy", talent="notice")
        original_dice = mistaken["check"]["dice"]
        self.call("correct", expected_revision=self.revision(), request_id="scenario-repair", target_action_id=mistaken["action_id"], reason="Mara said inspect it", replacement_check={"intent": "Inspect the remedy", "stakes": "Its contents may remain unknown", "talent": "craft"})
        replacement = self.resume()["pending"]
        self.assertEqual(replacement["check"]["dice"], original_dice)
        self.commit(replacement, [], beat="Mara identifies the ingredients.")
        self.engine = Engine(self.data_dir, rng=FixedDice(1, 1))
        resumed = self.resume()
        self.assertEqual(resumed["character"]["health"], 6)
        self.assertEqual(resumed["character"]["talents"]["craft"], 2)
        self.assertEqual(self.record("restorative")["quantity"], 1)
        self.assertEqual(resumed["campaign"]["scene_id"], "shelter")
        self.assertTrue(self.record("rescue")["rewarded"])
        self.assertIsNone(resumed["pending"])


if __name__ == "__main__":
    unittest.main()
