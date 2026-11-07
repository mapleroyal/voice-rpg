"""Rules and contract boundaries, independent of SQLite and random outcomes."""

import copy
import unittest

from voice_rpg.contracts import (GameError, SCHEMAS, apply_patch, default_record,
    normalize_character, normalize_possessions, validate, validate_campaign, validate_json,
    validate_patch, validate_record)
from voice_rpg.rules import (RULES, advance_talent, apply_harm, consume_stock,
    heal, health_label, resolve_check, rest, rules_capsule)


class FixedDice:
    def __init__(self, *values):
        self.values = iter(values)
        self.calls = 0

    def randint(self, lower, upper):
        self.calls += 1
        return next(self.values)


def character():
    return normalize_character({"name": "Mara", "concept": "A practical hedge witch",
        "motivation": "Find her missing friend", "talents": ["Herbalism", "Read people", "Wayfinding"]})


def creation():
    return {"campaign": "marsh", "request_id": "create-1", "title": "The Marsh",
        "preferences": {"tone": "hopeful", "boundaries": ["No graphic injury"]},
        "character": {"concept": "A practical hedge witch", "motivation": "Find a friend",
                      "talents": ["Herbalism", "Read people", "Wayfinding"]},
        "opening": {"situation": "A lantern floats downstream."}}


class RulesTests(unittest.TestCase):
    def test_initial_character_has_actual_mechanics(self):
        pc = character()
        self.assertEqual(pc["id"], "pc")
        self.assertEqual(pc["health"], 6)
        self.assertEqual(list(pc["talents"].values()), [1, 1, 1])
        self.assertEqual(normalize_character(creation()["character"])["name"], "You")

    def test_untrained_thresholds_and_rng_injection(self):
        for dice, total, tier in [([3, 3], 6, "setback"), ([3, 4], 7, "mixed"),
                                  ([4, 5], 9, "mixed"), ([5, 5], 10, "success")]:
            with self.subTest(dice=dice):
                rng = FixedDice(*dice)
                result = resolve_check(character=character(), rng=rng)
                self.assertEqual((result["total"], result["tier"]), (total, tier))
                self.assertIsNone(result["talent"])
                self.assertEqual(result["modifier"], 0)
                self.assertEqual(rng.calls, 2)

    def test_trained_difficulty_and_corrected_dice_reuse(self):
        for difficulty, total in [("easy", 8), ("standard", 7), ("hard", 6)]:
            rng = FixedDice()  # Any accidental reroll would exhaust this source.
            result = resolve_check("Herbalism", difficulty, character(), dice=[3, 3], rng=rng)
            self.assertEqual(result["total"], total)
            self.assertEqual(result["dice"], [3, 3])
            self.assertEqual(rng.calls, 0)
        pc = character()
        pc["talents"] = advance_talent(pc["talents"], "Herbalism")
        self.assertEqual(resolve_check("Herbalism", "hard", pc, dice=[3, 3])["total"], 7)

    def test_unknown_talent_fails_with_known_choices(self):
        with self.assertRaises(GameError) as caught:
            resolve_check("Herbalsm", character=character(), dice=[6, 6])
        self.assertIn("Herbalism", caught.exception.details["known_talents"])

    def test_bad_dice_and_rule_values_fail(self):
        for dice in ([0, 3], [7, 3], [True, 3], [3.0, 3], [3], (3, 3)):
            with self.subTest(dice=dice), self.assertRaises(GameError):
                resolve_check(character=character(), dice=dice)
        for difficulty in (None, {}, True, "impossible"):
            with self.subTest(difficulty=difficulty), self.assertRaises(GameError):
                resolve_check(character=character(), difficulty=difficulty, dice=[3, 3])

    def test_health_harm_healing_and_incapacity(self):
        self.assertEqual([health_label(h) for h in range(7)],
            ["incapacitated", "badly hurt", "badly hurt", "hurt", "hurt", "hurt", "well"])
        self.assertEqual([apply_harm(6, level) for level in ("minor", "moderate", "severe")], [5, 4, 3])
        self.assertEqual(apply_harm(1, "severe"), 0)
        self.assertEqual(heal(0), 2)
        self.assertEqual(heal(5), 6)
        self.assertEqual(rest(), 6)
        for health in (-1, 7, True, 1.0):
            with self.subTest(health=health), self.assertRaises(GameError):
                heal(health)

    def test_stock_cannot_be_overdrawn_or_consumed_for_free(self):
        self.assertEqual(consume_stock(2, 1), 1)
        self.assertEqual(consume_stock(2, 2), 0)
        for quantity, amount in [(1, 2), (1, 0), (1, -1), (1, True), (True, 1)]:
            with self.subTest(quantity=quantity, amount=amount), self.assertRaises(GameError):
                consume_stock(quantity, amount)

    def test_advancement_is_one_rating_and_caps_at_two(self):
        original = character()["talents"]
        improved = advance_talent(original, "Herbalism")
        self.assertEqual(improved["Herbalism"], 2)
        self.assertEqual(original["Herbalism"], 1)
        self.assertEqual(advance_talent(original, "Sailing")["Sailing"], 1)
        for talents, name in [(improved, "Herbalism"), (original, "herbalism"), (original, " ")]:
            with self.subTest(name=name), self.assertRaises(GameError):
                advance_talent(talents, name)

    def test_profile_capsule_is_independent_data(self):
        capsule = rules_capsule()
        self.assertIn("meaning", capsule["tiers"][0])
        self.assertNotIn("dispositions", capsule["tiers"][0])
        capsule["health"]["maximum"] = 99
        self.assertEqual(RULES["health"]["maximum"], 6)


class ContractTests(unittest.TestCase):
    def test_only_eight_tools_and_original_request_stays_unchanged(self):
        self.assertEqual(set(SCHEMAS), {"resume", "create", "lookup", "act", "begin", "commit", "cancel", "correct"})
        request = creation()
        original = copy.deepcopy(request)
        validate("create", request)
        self.assertEqual(request, original)
        self.assertNotIn("possessions", request)
        validate("resume", {})
        validate("resume", {"audience": "player", "include_guide": True})

    def test_creation_talents_are_three_distinct_names_not_ratings(self):
        for talents in (["a", "b"], ["a", "b", "c", "d"], ["a", "a", "c"],
                        ["a", " A ", "c"], {"a": 1, "b": 1, "c": 1}):
            request = creation()
            request["character"]["talents"] = talents
            with self.subTest(talents=talents), self.assertRaises(GameError):
                validate("create", request)

    def test_default_and_deprived_loadouts_have_fixed_mechanics(self):
        items = normalize_possessions()
        self.assertEqual([(item["quantity"], item["use"]) for item in items], [(1, None), (2, "heal")])
        self.assertEqual(normalize_possessions([]), [])
        custom = normalize_possessions([{"template": "item", "id": "letter", "name": "Letter"}])
        self.assertEqual(custom[0]["quantity"], 1)
        for template in ("gear", "restorative"):
            with self.subTest(template=template), self.assertRaises(GameError):
                normalize_possessions([{"template": template, "id": "kit", "name": "Kit", "quantity": 99}])

    def test_new_records_cannot_smuggle_mechanics_or_retirement(self):
        for record in (
            {"id": "other", "kind": "character", "name": "Other"},
            {"id": "npc", "kind": "npc", "name": "NPC", "archived": True},
            {"id": "goal", "kind": "goal", "text": "Find it", "rewarded": True},
            {"id": "goal", "kind": "goal", "text": "Find it", "status": "resolved"},
            {"id": "item", "kind": "item", "name": "Item", "quantity": 0},
            {"id": "npc", "kind": "npc", "name": "NPC", "details": {"secret": "x"}},
        ):
            with self.subTest(record=record), self.assertRaises(GameError):
                default_record(record)

    def test_protected_fields_cannot_be_patched_even_with_null(self):
        records = [character(), default_record({"id": "kit", "kind": "item", "name": "Kit"}),
                   default_record({"id": "goal", "kind": "goal", "text": "Find a way"})]
        for record, forbidden in zip(records, (("health", "talents", "id", "kind"),
                ("quantity", "holder", "use"), ("status", "milestone", "rewarded"))):
            for field in forbidden:
                with self.subTest(kind=record["kind"], field=field), self.assertRaises(GameError):
                    validate_patch(record, {field: None})

    def test_patch_preserves_omitted_fields_and_null_is_explicit(self):
        scene = default_record({"id": "opening", "kind": "scene", "situation": "Rain", "place": "bridge"})
        changed = apply_patch(scene, {"place": None})
        self.assertEqual(changed["situation"], "Rain")
        self.assertIsNone(changed["place"])
        self.assertEqual(scene["place"], "bridge")
        for field in ("situation", "present", "visibility"):
            with self.subTest(field=field), self.assertRaises(GameError):
                apply_patch(scene, {field: None})
        validate("act", {"campaign": "marsh", "expected_revision": 0, "request_id": "clear",
            "changes": [{"op": "patch_record", "id": "n", "changes": {"text": ""}}]})
        with self.assertRaises(GameError):
            validate_patch(default_record({"id": "n", "kind": "npc", "name": "N"}), {"situation": "Rain"})

    def test_fact_claim_has_subjects_and_attributed_source(self):
        fact = {"id": "claim", "kind": "fact", "text": "The road is safe.",
                "subjects": ["road"], "truth": "claim", "source": "guide"}
        record = default_record(fact)
        with self.assertRaises(GameError):
            apply_patch(record, {"source": None})
        truth = apply_patch(record, {"truth": "established", "source": None})
        self.assertEqual(truth["truth"], "established")
        with self.assertRaises(GameError):
            default_record({key: value for key, value in fact.items() if key != "subjects"})

    def test_empty_mutations_extra_fields_and_old_disposition_are_rejected(self):
        act = {"campaign": "marsh", "expected_revision": 0, "request_id": "act-1", "changes": []}
        with self.assertRaises(GameError):
            validate("act", act)
        validate("act", dict(act, beat="The boat bell rings."))
        commit = {"campaign": "marsh", "action_id": "action-1", "changes": [], "beat": "The door opens."}
        validate("commit", commit)
        with self.assertRaises(GameError):
            validate("commit", dict(commit, disposition="achieved"))
        with self.assertRaises(GameError):
            validate("act", dict(act, changes=[{"op": "harm", "severity": "minor", "amount": 99}]))

    def test_lookup_is_a_strict_union_with_bounded_pages(self):
        validate("lookup", {"campaign": "marsh", "selector": {"by": "holder", "holder": "pc", "limit": 100}})
        for selector in ({"by": "history", "limit": 101}, {"by": "history", "limit": True},
                         {"by": "status", "action_id": "a", "request_id": "r"},
                         {"by": "ids", "ids": []}, {"by": "search", "query": "x", "sql": "anything"}):
            with self.subTest(selector=selector), self.assertRaises(GameError):
                validate("lookup", {"campaign": "marsh", "selector": selector})

    def test_campaign_metadata_is_validated_for_restoration(self):
        campaign = {"id": "marsh", "title": "The Marsh", "revision": 0,
                    "scene_id": "opening", "character_id": "pc", "preferences": {},
                    "save_version": 2, "rules_version": "2"}
        validate_campaign(campaign)
        for field, value in (("revision", True), ("revision", -1), ("character_id", "other"),
                             ("save_version", 1), ("rules_version", "1"), ("id", "marsh\n")):
            with self.subTest(field=field, value=value), self.assertRaises(GameError):
                validate_campaign(dict(campaign, **{field: value}))
        validate("lookup", {"campaign": "marsh", "selector": {"by": "context", "section": "inventory"}})
        with self.assertRaises(GameError):
            validate("lookup", {"campaign": "marsh", "selector": {"by": "context", "section": "arbitrary"}})

    def test_numeric_types_and_raw_json_are_strict(self):
        begin = {"campaign": "marsh", "expected_revision": 0, "request_id": "b", "intent": "Cross", "stakes": "The rope may break"}
        for value in (True, 0.0, -1):
            with self.subTest(value=value), self.assertRaises(GameError):
                validate("begin", dict(begin, expected_revision=value))
        validate("begin", dict(begin, talent=None))
        cyclic = {}; cyclic["self"] = cyclic
        deep = {}
        for _ in range(70):
            deep = {"inner": deep}
        for value in (cyclic, deep, {"x": float("nan")}, {"x": float("inf")}, {1: "bad key"}):
            with self.assertRaises(GameError):
                validate_json(value)


if __name__ == "__main__":
    unittest.main()
