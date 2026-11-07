"""Command contracts, bounded JSON validation, and record normalization."""

import copy
import json
import math
import re

from .rules import RULES


VERSION = "2.2.1"
SAVE_VERSION = 2
MAX_JSON_BYTES = 128 * 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 4000000
MAX_STRING_BYTES = 1024 * 1024
MAX_INTEGER = 9007199254740991


class GameError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = {} if details is None else details


def _error(message, path, **details):
    raise GameError("invalid_input", message, dict(details, path=path))


def validate_json(value, path="$", max_depth=MAX_JSON_DEPTH):
    """Reject non-JSON Python values and bound work before schema traversal."""
    count = 0
    ancestry = set()

    def visit(item, depth, at):
        nonlocal count
        count += 1
        if depth > max_depth or count > MAX_JSON_NODES:
            _error("JSON exceeds the supported depth or number of values.", at)
        kind = type(item)
        if kind is str:
            try:
                size = len(item.encode("utf-8"))
            except UnicodeEncodeError:
                _error("Strings must contain valid Unicode.", at)
            if size > MAX_STRING_BYTES:
                _error("JSON string is too large.", at)
        elif kind is int:
            if abs(item) > MAX_INTEGER:
                _error("Integer is outside the supported exact JSON range.", at)
        elif kind is float:
            if not math.isfinite(item):
                _error("JSON numbers must be finite.", at)
        elif kind in (bool, type(None)):
            pass
        elif kind in (dict, list):
            identity = id(item)
            if identity in ancestry:
                _error("JSON cannot contain circular references.", at)
            ancestry.add(identity)
            if kind is dict:
                for key, child in item.items():
                    if type(key) is not str:
                        _error("JSON object keys must be strings.", at)
                    visit(key, depth + 1, at)
                    visit(child, depth + 1, at + "." + key)
            else:
                for index, child in enumerate(item):
                    visit(child, depth + 1, "%s[%d]" % (at, index))
            ancestry.remove(identity)
        else:
            _error("Value is not a supported JSON type.", at)

    visit(value, 0, path)
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")) > MAX_JSON_BYTES:
        _error("JSON payload exceeds the supported size.", path)


def _same(left, right):
    # Python equates True and 1, whereas JSON-schema const/enum must not.
    return type(left) is type(right) and left == right


def _matches_type(value, name):
    return {
        "object": type(value) is dict, "array": type(value) is list,
        "string": type(value) is str, "integer": type(value) is int,
        "number": type(value) in (int, float), "boolean": type(value) is bool,
        "null": value is None,
    }[name]


def _union_options(value, options, path):
    """Select only branches excluded by a required constant, never by a guess."""
    if type(value) is not dict or not options:
        return options, None
    for field in options[0].get("required", []):
        if not all(option.get("type") == "object" and field in option.get("required", [])
                   and "const" in option.get("properties", {}).get(field, {}) for option in options):
            continue
        choices = []
        for option in options:
            choice = option["properties"][field]["const"]
            if not any(_same(choice, existing) for existing in choices):
                choices.append(choice)
        selected = [option for option in options if field in value
                    and _same(value[field], option["properties"][field]["const"])]
        if not selected:
            problem = "Missing required field: " if field not in value else "Invalid "
            _error(problem + field + ". Valid choices: " +
                   ", ".join(json.dumps(choice, ensure_ascii=False) for choice in choices) + ".",
                   path + "." + field, discriminator=field, allowed_values=choices)
        return selected, field
    return options, None


def _validate(value, schema, path):
    for mode in ("oneOf", "anyOf"):
        if mode in schema:
            options, discriminator = _union_options(value, schema[mode], path)
            matched = 0
            errors = []
            for option in options:
                try:
                    _validate(value, option, path)
                    matched += 1
                except GameError as exc:
                    errors.append((option, exc))
            if matched == 0:
                if discriminator:
                    if len(errors) == 1:
                        raise errors[0][1]
                    _error("Value does not match an allowed " + discriminator +
                           " variant. Choose one of the listed alternatives.", path,
                           discriminator=discriminator, alternatives=[
                               dict(error.details, message=error.message,
                                    required_fields=list(option.get("required", [])))
                               for option, error in errors])
                _error("Value does not match an allowed schema.", path)
            if mode == "oneOf" and matched != 1:
                _error("Value matches more than one allowed schema; exactly one is required.", path)
    if "type" in schema:
        kinds = schema["type"] if type(schema["type"]) is list else [schema["type"]]
        if not any(_matches_type(value, kind) for kind in kinds):
            _error("Expected " + " or ".join(kinds) + ".", path)
    if "const" in schema and not _same(value, schema["const"]):
        _error("Value does not match the required constant.", path)
    if "enum" in schema and not any(_same(value, option) for option in schema["enum"]):
        _error("Value is not one of the allowed choices.", path)
    if type(value) is dict:
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        missing = [key for key in schema.get("required", []) if key not in value]
        unknown = [key for key in value if key not in properties] if extra is False else []
        if missing or unknown:
            messages = []
            for label, fields in (("Missing required", missing), ("Unknown", unknown)):
                if fields:
                    messages.append(label + (" fields: " if len(fields) > 1 else " field: ") + ", ".join(fields) + ".")
            _error(" ".join(messages), path + "." + (missing or unknown)[0],
                   missing_fields=missing, unknown_fields=unknown, allowed_fields=list(properties))
        if len(value) < schema.get("minProperties", 0) or len(value) > schema.get("maxProperties", MAX_JSON_NODES):
            _error("Object has an unsupported number of fields.", path)
        for key, child in value.items():
            if "propertyNames" in schema:
                _validate(key, schema["propertyNames"], path + ".<key>")
            if key in properties:
                _validate(child, properties[key], path + "." + key)
            elif type(extra) is dict:
                _validate(child, extra, path + "." + key)
    elif type(value) is list:
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", MAX_JSON_NODES):
            _error("Array has an unsupported number of values.", path)
        if schema.get("uniqueItems"):
            keys = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(set(keys)) != len(keys):
                _error("Array values must be unique.", path)
        if "items" in schema:
            for index, child in enumerate(value):
                _validate(child, schema["items"], "%s[%d]" % (path, index))
    elif type(value) is str:
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", MAX_STRING_BYTES):
            _error("String has an unsupported length.", path)
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            _error("String does not match the required format.", path)
    elif type(value) in (int, float):
        if ("minimum" in schema and value < schema["minimum"]) or ("maximum" in schema and value > schema["maximum"]):
            _error("Number is outside the allowed range.", path)


def validate_value(value, schema, path="$"):
    validate_json(value, path)
    _validate(value, schema, path)


def _object(properties, required=(), **extra):
    return dict({"type": "object", "properties": properties,
                 "required": list(required), "additionalProperties": False}, **extra)


ID_SCHEMA = {"type": "string", "pattern": r"^[a-z][a-z0-9_-]{0,63}$(?![\s\S])", "maxLength": 64}
HOLDER_KINDS = ("character", "npc", "place", "scene")
HOLDER_SCHEMA = dict(ID_SCHEMA, description="ID of an existing record of kind " +
    ", ".join(HOLDER_KINDS) + ". Locations that contain inventory use place records.")
NONEMPTY = {"type": "string", "minLength": 1, "pattern": r"\S"}
NAME = dict(NONEMPTY, maxLength=200)
TALENT = dict(NONEMPTY, maxLength=80)
SHORT = dict(NONEMPTY, maxLength=1000)
TEXT = {"type": "string", "maxLength": 4000}
REQUEST = dict(NONEMPTY, maxLength=128)
VISIBILITY = {"enum": ["player", "gm"], "default": "player"}
AUDIENCE = {"enum": ["gm", "player"], "default": "gm"}
NULL_ID = {"anyOf": [ID_SCHEMA, {"type": "null"}]}
IDS = {"type": "array", "items": ID_SCHEMA, "uniqueItems": True, "maxItems": 100}
PRESENT_SCHEMA = dict(IDS, description="Present NPC or player-character IDs only. Nearby items are derived from their holder, not listed here.")
BOOL = {"type": "boolean"}
QUANTITY = {"type": "integer", "minimum": 1, "maximum": 1000000}
PREFERENCES_SCHEMA = _object({"genre": NAME, "tone": NAME,
    "boundaries": {"type": "array", "items": SHORT, "maxItems": 30, "uniqueItems": True}})
CHECK_FIELDS = {"intent": SHORT, "stakes": SHORT,
                "talent": {"anyOf": [TALENT, {"type": "null"}]},
                "difficulty": {"enum": list(RULES["difficulties"]), "default": RULES["default_difficulty"]}}
REPLACEMENT_CHECK_SCHEMA = _object(CHECK_FIELDS, ("intent", "stakes"))
CHARACTER_INPUT_SCHEMA = _object({"name": dict(NAME, default="You"), "concept": SHORT, "motivation": SHORT,
    "talents": {"type": "array", "items": TALENT, "minItems": RULES["talents"]["initial_count"],
                "maxItems": RULES["talents"]["initial_count"], "uniqueItems": True}},
    ("concept", "motivation", "talents"))

# These are exactly the editable narrative fields. Identity and mechanical fields never appear here.
PATCH_FIELDS = {
    "character": {"name": NAME, "concept": SHORT, "motivation": SHORT},
    "npc": {"name": NAME, "text": TEXT, "visibility": VISIBILITY},
    "place": {"name": NAME, "text": TEXT, "visibility": VISIBILITY},
    "scene": {"situation": SHORT, "place": NULL_ID, "present": PRESENT_SCHEMA, "visibility": VISIBILITY},
    "item": {"name": NAME, "text": TEXT, "visibility": VISIBILITY},
    "fact": {"text": SHORT, "subjects": dict(IDS, minItems=1), "truth": {"enum": ["established", "claim"]},
             "source": NULL_ID, "visibility": VISIBILITY},
    "goal": {"text": SHORT, "subjects": IDS, "visibility": VISIBILITY},
}
RECORD_FIELDS = {
    "character": dict(PATCH_FIELDS["character"], talents={"type": "object", "minProperties": RULES["talents"]["initial_count"],
        "maxProperties": 100, "propertyNames": TALENT,
        "additionalProperties": {"type": "integer", "minimum": 1, "maximum": RULES["talents"]["maximum"]}},
        health={"type": "integer", "minimum": 0, "maximum": RULES["health"]["maximum"]}),
    "npc": dict(PATCH_FIELDS["npc"], archived=BOOL),
    "place": dict(PATCH_FIELDS["place"], archived=BOOL),
    "scene": dict(PATCH_FIELDS["scene"], archived=BOOL),
    "item": dict(PATCH_FIELDS["item"], holder=HOLDER_SCHEMA,
                 quantity={"type": "integer", "minimum": 0, "maximum": QUANTITY["maximum"]},
                 use={"enum": [None, "heal"]}),
    "fact": dict(PATCH_FIELDS["fact"], archived=BOOL),
    "goal": dict(PATCH_FIELDS["goal"], status={"enum": ["active", "resolved", "abandoned"]},
                 milestone=BOOL, rewarded=BOOL),
}
# Public projection is explicit: a new stored field is private until listed here.
PUBLIC_FIELDS = {
    "character": ("id", "kind", "name", "concept", "motivation", "talents", "health"),
    "npc": ("id", "kind", "name", "text", "archived"),
    "place": ("id", "kind", "name", "text", "archived"),
    "scene": ("id", "kind", "situation", "place", "present", "archived"),
    "item": ("id", "kind", "name", "text", "holder", "quantity", "use"),
    "fact": ("id", "kind", "text", "subjects", "truth", "source", "archived"),
    "goal": ("id", "kind", "text", "subjects", "status", "milestone", "rewarded"),
}
CAMPAIGN_SCHEMA = _object({
    "id": ID_SCHEMA, "title": NAME, "revision": {"type": "integer", "minimum": 0},
    "scene_id": ID_SCHEMA, "character_id": {"const": "pc"},
    "preferences": PREFERENCES_SCHEMA, "save_version": {"const": SAVE_VERSION},
    "rules_version": {"const": RULES["version"]},
}, ("id", "title", "revision", "scene_id", "character_id", "preferences", "save_version", "rules_version"))


RECORD_DEFAULTS = {
    "npc": {"text": "", "visibility": "player", "archived": False},
    "place": {"text": "", "visibility": "player", "archived": False},
    "scene": {"place": None, "present": [], "visibility": "player", "archived": False},
    "item": {"text": "", "visibility": "player", "holder": "pc", "quantity": 1, "use": None},
    "fact": {"truth": "established", "source": None, "visibility": "player", "archived": False},
    "goal": {"subjects": [], "status": "active", "milestone": False, "rewarded": False, "visibility": "player"},
}
CREATE_RECORD_SCHEMAS = {}
STORED_RECORD_SCHEMAS = {}
for _kind, _fields in RECORD_FIELDS.items():
    _base = {"id": ID_SCHEMA, "kind": {"const": _kind}}
    STORED_RECORD_SCHEMAS[_kind] = _object(dict(_base, **_fields), list(_base) + list(_fields))
    if _kind == "character":
        continue
    _create_fields = {field: dict(schema, default=copy.deepcopy(RECORD_DEFAULTS[_kind][field]))
                      if field in RECORD_DEFAULTS[_kind] else schema
                      for field, schema in _fields.items()}
    for _protected in ("archived", "status", "rewarded"):
        _create_fields.pop(_protected, None)
    if _kind == "item":
        _create_fields["quantity"] = QUANTITY
    _required = ["id", "kind"] + [field for field in _create_fields if field not in RECORD_DEFAULTS[_kind]]
    CREATE_RECORD_SCHEMAS[_kind] = _object(dict(_base, **_create_fields), _required)
CREATE_RECORD_SCHEMA = {"oneOf": list(CREATE_RECORD_SCHEMAS.values())}
RECORD_SCHEMA = {"oneOf": list(STORED_RECORD_SCHEMAS.values())}


def _possession(template):
    fields = {"template": {"const": template}, "id": ID_SCHEMA, "name": NAME, "text": TEXT}
    if template == "item":
        fields["quantity"] = QUANTITY
    return _object(fields, ("template", "id", "name"))


POSSESSIONS_SCHEMA = {"type": "array", "maxItems": 100,
    "items": {"oneOf": [_possession(template) for template in ("gear", "restorative", "item")]}}
OPENING_SCHEMA = _object({"situation": SHORT, "place": NULL_ID, "present": PRESENT_SCHEMA,
    "records": {"type": "array", "items": CREATE_RECORD_SCHEMA, "maxItems": 100}}, ("situation",))


def _op(name, fields=None, required=(), description=None):
    schema = _object(dict({"op": {"const": name}}, **(fields or {})), ["op"] + list(required))
    if description:
        schema["description"] = description
    return schema


# Kind-specific validation follows syntax validation because patch requests refer to stored IDs.
_PATCH_UNION = {}
for _fields in PATCH_FIELDS.values():
    for _field, _schema in _fields.items():
        if _field not in _PATCH_UNION:
            _PATCH_UNION[_field] = _schema
        elif _PATCH_UNION[_field] != _schema:
            _options = _PATCH_UNION[_field].get("anyOf", [_PATCH_UNION[_field]])
            if _schema not in _options:
                _PATCH_UNION[_field] = {"anyOf": _options + [_schema]}
OPERATION_SCHEMA = {"oneOf": [
    _op("create_record", {"record": CREATE_RECORD_SCHEMA}, ("record",)),
    _op("patch_record", {"id": ID_SCHEMA, "changes": _object(_PATCH_UNION, minProperties=1)}, ("id", "changes")),
    _op("retire_record", {"id": ID_SCHEMA}, ("id",)),
    _op("set_scene", {"id": ID_SCHEMA}, ("id",)),
    _op("update_preferences", {"changes": dict(PREFERENCES_SCHEMA, minProperties=1)}, ("changes",)),
    _op("harm", {"severity": {"enum": list(RULES["health"]["harm"])}}, ("severity",)),
    _op("rest", description="Restore the PC when the profile rest requirement is satisfied."),
    _op("use_item", {"id": ID_SCHEMA}, ("id",), description="Consume one unit of a PC-held restorative item with use=heal and apply profile healing atomically. Treatment must be fictionally possible."),
    _op("consume_item", {"id": ID_SCHEMA, "quantity": QUANTITY,
                          "expected_holder": dict(HOLDER_SCHEMA, default="pc")}, ("id", "quantity")),
    _op("transfer_item", {"id": ID_SCHEMA, "to": HOLDER_SCHEMA, "quantity": QUANTITY,
                          "expected_holder": dict(HOLDER_SCHEMA, default="pc")}, ("id", "to", "quantity")),
    _op("resolve_goal", {"id": ID_SCHEMA}, ("id",)),
    _op("abandon_goal", {"id": ID_SCHEMA}, ("id",)),
    _op("advance", {"id": ID_SCHEMA, "talent": TALENT}, ("id", "talent"), description="Claim the goal identified by id: an unclaimed resolved milestone that existed before this transaction. Choose a new talent or improve an eligible existing talent; reward and improvement change atomically."),
]}
CHANGES_SCHEMA = {"type": "array", "items": OPERATION_SCHEMA, "maxItems": 100}
LIMIT_SCHEMA = {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}


def _selector(by, fields=None, required=(), paginate=True):
    props = dict({"by": {"const": by}}, **(fields or {}))
    if paginate:
        props["limit"] = LIMIT_SCHEMA
    return _object(props, ["by"] + list(required))


SELECTOR_SCHEMA = {"oneOf": [
    _selector("ids", {"ids": dict(IDS, minItems=1)}, ("ids",)),
    _selector("subjects", {"subjects": dict(IDS, minItems=1)}, ("subjects",)),
    _selector("holder", {"holder": HOLDER_SCHEMA}, ("holder",)),
    _selector("search", {"query": dict(NONEMPTY, maxLength=200)}, ("query",)),
    _selector("history"),
    _selector("context", {"section": {"enum": ["inventory", "nearby_items", "present", "goals", "facts"]}}, ("section",)),
    _selector("status", {"action_id": REQUEST}, ("action_id",), False),
    _selector("status", {"request_id": REQUEST}, ("request_id",), False),
]}


def _command(description, fields, required):
    return _object(fields, required, description=description)


_WORLD_WRITE = {"campaign": ID_SCHEMA, "expected_revision": {"type": "integer", "minimum": 0}, "request_id": REQUEST}
SCHEMAS = {
    "resume": _command("Load saved play and the full canonical narrator guide at call start or after a revision/recovery conflict. With no campaign, select the sole campaign or return a picker; never create implicitly. Set include_guide=false only when the full guide is already available in this conversation.",
        {"campaign": ID_SCHEMA, "audience": AUDIENCE, "include_guide": {"type": "boolean", "default": True}}, ()),
    "create": _command("Create one immediately playable campaign atomically. Supply accepted concept, motivation, exactly three distinct talent names and an opening. Omitted possessions uses the profile loadout; supplied entries replace it, including an empty list for a deprived opening. gear/restorative template quantities and healing come from rules. Retrying identical request_id and original input recovers acceptance; other creation of an existing campaign fails.",
        {"campaign": ID_SCHEMA, "request_id": REQUEST, "title": NAME, "preferences": PREFERENCES_SCHEMA,
         "character": CHARACTER_INPUT_SCHEMA, "opening": OPENING_SCHEMA, "possessions": POSSESSIONS_SCHEMA},
        ("campaign", "request_id", "title", "preferences", "character", "opening")),
    "lookup": _command("Fetch only missing context or recovery status. Choose one selector; pages default to 20 and stop at 100. Cursors belong to the original selector and campaign revision; a changed revision requires a fresh page. Status selects exactly one action_id or request_id.",
        {"campaign": ID_SCHEMA, "selector": SELECTOR_SCHEMA, "cursor": dict(NONEMPTY, maxLength=4000), "audience": AUDIENCE}, ("campaign", "selector")),
    "act": _command("Apply known consequences or establish fiction with no pending check. Requires at least one change or a short public beat. Requests are deduplicated by the original payload before checking expected_revision; identical retries recover acceptance, never repeat effects. Omitted patch fields survive; supplied values replace the field. Mechanical fields require named effects.",
        dict(_WORLD_WRITE, changes=CHANGES_SCHEMA, beat=SHORT), tuple(_WORLD_WRITE) + ("changes",)),
    "begin": _command("Persist one uncertain intention and its single player-character check. No pending attempt may exist. Standard difficulty and untrained talent are defaults. The accepted mutation advances revision; use its saved action_id to commit or explicitly cancel. Exact request_id retries preserve the roll. Unknown talents fail with known choices.",
        dict(_WORLD_WRITE, **CHECK_FIELDS), tuple(_WORLD_WRITE) + ("intent", "stakes")),
    "commit": _command("Complete the current pending action atomically using its action_id. Supply only realized changes and/or one short public beat consistent with the saved tier. Identical closing payload retries recover acceptance, while changed closings conflict. A rejected commit leaves the attempt and dice pending so corrected changes can be submitted. A historical replay never supplies stale deltas as current truth.",
        {"campaign": ID_SCHEMA, "action_id": REQUEST, "changes": CHANGES_SCHEMA, "beat": SHORT}, ("campaign", "action_id", "changes")),
    "cancel": _command("Explicitly abandon the current pending attempt, retaining its saved roll and audit. This advances revision and is never timeout recovery. Identical closing retries recover acceptance; changed or competing closings conflict.",
        {"campaign": ID_SCHEMA, "action_id": REQUEST, "reason": SHORT}, ("campaign", "action_id", "reason")),
    "correct": _command("Repair the current pending attempt or latest completed gameplay action; creation/corrections are not targets and unrelated pending work blocks an older correction. Restore before-images without rewinding revision. replacement_check atomically opens the corrected uncertain intention, reusing original raw dice when available. Omit it for a deterministic replacement. Exact request_id retries recover the same correction.",
        dict(_WORLD_WRITE, target_action_id=REQUEST, reason=SHORT, replacement_check=REPLACEMENT_CHECK_SCHEMA),
        tuple(_WORLD_WRITE) + ("target_action_id", "reason")),
}


def _distinct_talents(names):
    normalized = [name.casefold().strip() for name in names]
    if len(normalized) != len(set(normalized)):
        _error("Talent names must be distinct, including capitalization and surrounding spaces.", "$.talents")


def validate_campaign(campaign):
    validate_value(campaign, CAMPAIGN_SCHEMA, "$.campaign")


def validate_preferences(preferences):
    validate_value(preferences, PREFERENCES_SCHEMA, "$.preferences")


def validate_record(record):
    validate_value(record, RECORD_SCHEMA, "$.record")
    if record["kind"] == "character":
        if record["id"] != "pc":
            _error("The sole player character has ID pc.", "$.record.id")
        _distinct_talents(record["talents"])
    if record["kind"] == "fact" and record["truth"] == "claim" and record["source"] is None:
        _error("A claim requires a source record ID.", "$.record.source")
    if record["kind"] == "goal" and record["rewarded"] and (not record["milestone"] or record["status"] != "resolved"):
        _error("Only a resolved milestone can have a claimed reward.", "$.record.rewarded")


def default_record(record):
    validate_value(record, CREATE_RECORD_SCHEMA, "$.record")
    normalized = copy.deepcopy(RECORD_DEFAULTS[record["kind"]])
    normalized.update(copy.deepcopy(record))
    validate_record(normalized)
    return normalized


def normalize_character(character):
    validate_value(character, CHARACTER_INPUT_SCHEMA, "$.character")
    _distinct_talents(character["talents"])
    record = {"id": "pc", "kind": "character", "name": character.get("name", CHARACTER_INPUT_SCHEMA["properties"]["name"]["default"]),
              "concept": character["concept"], "motivation": character["motivation"],
              "talents": {talent: RULES["talents"]["initial_rating"] for talent in character["talents"]},
              "health": RULES["health"]["maximum"]}
    validate_record(record)
    return record


def normalize_possessions(possessions=None):
    if possessions is None:
        possessions = [{"template": "gear", "id": "signature_tool", "name": "Signature tool"},
                       {"template": "restorative", "id": "restorative", "name": "Restorative"}]
    validate_value(possessions, POSSESSIONS_SCHEMA, "$.possessions")
    records = []
    seen = set()
    for possession in possessions:
        if possession["id"] in seen:
            _error("Possession IDs must be distinct.", "$.possessions")
        seen.add(possession["id"])
        template = possession["template"]
        quantity = (RULES["starting_gear_quantity"] if template == "gear" else
                    RULES["starting_restorative_quantity"] if template == "restorative" else possession.get("quantity", 1))
        records.append(default_record({"id": possession["id"], "kind": "item", "name": possession["name"],
            "text": possession.get("text", ""), "holder": "pc", "quantity": quantity,
            "use": "heal" if template == "restorative" else None}))
    return records


def validate_patch(record, changes):
    validate_record(record)
    validate_value(changes, _object(PATCH_FIELDS[record["kind"]], minProperties=1), "$.changes")
    result = copy.deepcopy(record)
    result.update(copy.deepcopy(changes))
    validate_record(result)


def apply_patch(record, changes):
    validate_patch(record, changes)
    result = copy.deepcopy(record)
    result.update(copy.deepcopy(changes))
    return result


def validate(command, args):
    if type(command) is not str or command not in SCHEMAS:
        raise GameError("unknown_command", "Unknown command.")
    validate_value(args, SCHEMAS[command])
    if command == "create":
        normalize_character(args["character"])
        normalize_possessions(args.get("possessions"))
        for record in args["opening"].get("records", []):
            default_record(record)
    if command in ("act", "commit"):
        if not args["changes"] and not args.get("beat"):
            _error("At least one change or a public beat is required.", "$")
        for operation in args["changes"]:
            if operation["op"] == "create_record":
                default_record(operation["record"])
