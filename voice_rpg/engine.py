"""Transactional game state. The narrator supplies fiction; this module owns acceptance."""

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid
from contextlib import closing, contextmanager

from .contracts import (GameError, SAVE_VERSION, SCHEMAS, VERSION, apply_patch, default_record,
                        normalize_character, normalize_possessions, validate,
                        validate_campaign, validate_record, validate_json, validate_value,
                        CAMPAIGN_SCHEMA, CHECK_FIELDS, ID_SCHEMA, REQUEST, SHORT, PUBLIC_FIELDS,
                        HOLDER_KINDS)
from .rules import (RULES, advance_talent, apply_harm, consume_stock, heal, health_label,
                    resolve_check, rest, rules_capsule)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _id(prefix="a"):
    return prefix + "_" + uuid.uuid4().hex


def _fail(code, message, **details):
    raise GameError(code, message, details)


class _CallableRandom:
    def __init__(self, callback):
        self.callback = callback

    def randint(self, lower, upper):
        return self.callback()


class Engine:
    def __init__(self, data_dir, rng=None, *, initialize=True):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "game.sqlite"
        self.rng = _CallableRandom(rng) if callable(rng) and not hasattr(rng, "randint") else rng
        if initialize:
            self._initialize()

    def _connect(self):
        connection = sqlite3.connect(str(self.db_path), timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def _initialize(self):
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version not in (0, SAVE_VERSION) or (tables and version != SAVE_VERSION):
                _fail("unsupported_save", "This collection is not in the current save format.")
            statements = """
                CREATE TABLE IF NOT EXISTS campaigns (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS records (
                    campaign TEXT NOT NULL REFERENCES campaigns(id), id TEXT NOT NULL,
                    data TEXT NOT NULL, PRIMARY KEY(campaign,id));
                CREATE TABLE IF NOT EXISTS actions (
                    campaign TEXT NOT NULL REFERENCES campaigns(id), id TEXT NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL,
                    created_revision INTEGER NOT NULL, accepted_revision INTEGER NOT NULL,
                    data TEXT NOT NULL, closing_hash TEXT, closing_kind TEXT,
                    PRIMARY KEY(campaign,id));
                CREATE INDEX IF NOT EXISTS recent_actions ON actions(campaign,accepted_revision DESC);
                CREATE INDEX IF NOT EXISTS history_actions ON actions(campaign,created_revision);
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending
                    ON actions(campaign) WHERE status='pending';
                CREATE TABLE IF NOT EXISTS requests (
                    campaign TEXT NOT NULL REFERENCES campaigns(id), id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, command TEXT NOT NULL,
                    action_id TEXT NOT NULL, accepted_revision INTEGER NOT NULL,
                    PRIMARY KEY(campaign,id),
                    FOREIGN KEY(campaign,action_id) REFERENCES actions(campaign,id));
            """
            for statement in statements.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("PRAGMA user_version=%d" % SAVE_VERSION)
            self._validate_structure(db)
            db.commit()

    @staticmethod
    def _validate_structure(db):
        columns = {
            "campaigns": {"id", "data"}, "records": {"campaign", "id", "data"},
            "actions": {"campaign", "id", "kind", "status", "created_revision", "accepted_revision",
                        "data", "closing_hash", "closing_kind"},
            "requests": {"campaign", "id", "payload_hash", "command", "action_id", "accepted_revision"},
        }
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != set(columns):
            _fail("invalid_backup", "Collection table schema is invalid.")
        for table, required in columns.items():
            if {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")} != required:
                _fail("invalid_backup", "Collection column schema is invalid.", table=table)
        indexes = list(db.execute("PRAGMA index_list(actions)"))
        if not any(row[1] == "one_pending" and row[2] == 1 and row[4] == 1 for row in indexes):
            _fail("invalid_backup", "Collection pending-action constraint is missing.")

    @contextmanager
    def _transaction(self, write=False):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def dispatch(self, command, args):
        validate(command, args)
        args = copy.deepcopy(args)
        try:
            with self._transaction(command not in ("resume", "lookup")) as db:
                result = getattr(self, "_" + command)(db, args)
        except sqlite3.Error as exc:
            raise GameError("storage_error", "The collection could not complete this request; retry the same identity.",
                            {"detail": str(exc)}) from exc
        if command not in ("resume", "lookup") and not result.get("replayed"):
            try:
                self.backup()
            except (OSError, sqlite3.Error, GameError) as exc:
                result["warning"] = {"code": "backup_failed", "message": str(exc), "accepted": True}
        return result

    def _campaign(self, db, campaign):
        row = db.execute("SELECT data FROM campaigns WHERE id=?", (campaign,)).fetchone()
        if row is None:
            _fail("not_found", "Unknown campaign.", campaign=campaign)
        return json.loads(row[0])

    def _records(self, db, campaign):
        return {row[0]: json.loads(row[1]) for row in db.execute(
            "SELECT id,data FROM records WHERE campaign=? ORDER BY id", (campaign,))}

    def _action(self, db, campaign, action_id):
        row = db.execute("SELECT * FROM actions WHERE campaign=? AND id=?", (campaign, action_id)).fetchone()
        if row is None:
            _fail("not_found", "Unknown action.", action_id=action_id)
        return self._action_row(row)

    @staticmethod
    def _action_row(row):
        result = dict(row)
        result["data"] = json.loads(result["data"])
        return result

    def _pending(self, db, campaign):
        row = db.execute("SELECT * FROM actions WHERE campaign=? AND status='pending'", (campaign,)).fetchone()
        return None if row is None else self._action_row(row)

    def _latest(self, db, campaign):
        row = db.execute("SELECT * FROM actions WHERE campaign=? ORDER BY accepted_revision DESC, "
                         "(status='pending') DESC, rowid DESC LIMIT 1", (campaign,)).fetchone()
        return None if row is None else self._action_row(row)

    def _revision(self, db, meta, expected):
        if meta["revision"] != expected:
            pending, latest = self._pending(db, meta["id"]), self._latest(db, meta["id"])
            _fail("revision_conflict", "Saved state changed; resume before choosing a new action.",
                  current_revision=meta["revision"], pending_action_id=pending["id"] if pending else None,
                  last_action_id=latest["id"] if latest else None)

    def _no_pending(self, db, campaign):
        pending = self._pending(db, campaign)
        if pending:
            _fail("pending_action", "Complete, cancel, or correct the saved attempt first.", action_id=pending["id"])

    def _recover(self, db, command, args):
        row = db.execute("SELECT * FROM requests WHERE campaign=? AND id=?",
                         (args["campaign"], args["request_id"])).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != _hash([command, args]):
            _fail("request_conflict", "This request identity was already accepted with different input.")
        meta = self._campaign(db, args["campaign"])
        action = self._action(db, args["campaign"], row["action_id"])
        return self._receipt(db, meta, action, row["accepted_revision"], True)

    def _request(self, db, command, args, action_id, revision):
        db.execute("INSERT INTO requests VALUES(?,?,?,?,?,?)", (args["campaign"], args["request_id"],
                   _hash([command, args]), command, action_id, revision))

    def _insert_action(self, db, campaign, kind, revision, data, status="completed", action_id=None):
        action_id = action_id or _id()
        db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?)",
                   (campaign, action_id, kind, status, revision, revision, _json(data), None, None))
        return self._action(db, campaign, action_id)

    def _update_action(self, db, action):
        db.execute("UPDATE actions SET status=?,accepted_revision=?,data=?,closing_hash=?,closing_kind=? "
                   "WHERE campaign=? AND id=?", (action["status"], action["accepted_revision"], _json(action["data"]),
                   action["closing_hash"], action["closing_kind"], action["campaign"], action["id"]))

    def _save_world(self, db, meta, records, previous):
        db.execute("UPDATE campaigns SET data=? WHERE id=?", (_json(meta), meta["id"]))
        for identity in previous.keys() - records.keys():
            db.execute("DELETE FROM records WHERE campaign=? AND id=?", (meta["id"], identity))
        for identity, record in records.items():
            if previous.get(identity) != record:
                db.execute("INSERT INTO records(campaign,id,data) VALUES(?,?,?) "
                           "ON CONFLICT(campaign,id) DO UPDATE SET data=excluded.data",
                           (meta["id"], identity, _json(record)))

    @staticmethod
    def _images(old_meta, meta, old_records, records):
        changed = {identity for identity in old_records.keys() | records.keys()
                   if old_records.get(identity) != records.get(identity)}
        fields = {key for key in old_meta.keys() | meta.keys()
                  if key != "revision" and old_meta.get(key) != meta.get(key)}
        return {"before": {"records": {key: old_records.get(key) for key in sorted(changed)},
                           "campaign": {key: old_meta.get(key) for key in sorted(fields)}},
                "after": {"records": {key: records.get(key) for key in sorted(changed)},
                          "campaign": {key: meta.get(key) for key in sorted(fields)}}}

    @staticmethod
    def _summary(action):
        if action is None:
            return None
        result = {"action_id": action["id"], "kind": action["kind"], "status": action["status"],
                  "accepted_revision": action["accepted_revision"]}
        for key in ("intent", "stakes", "check", "beat", "reason", "target_action_id", "replacement_action_id", "replaces"):
            if key in action["data"]:
                result[key] = action["data"][key]
        return result

    def _receipt(self, db, meta, action, revision, replayed=False):
        result = {"campaign": meta["id"], "action_id": action["id"], "status": action["status"],
                  "accepted_revision": revision, "current_revision": meta["revision"], "replayed": replayed}
        if meta["revision"] > revision:
            result["refresh_required"] = True
            return result
        for key in ("check", "intent", "stakes", "beat", "reason", "replacement_action_id", "replaces"):
            if key in action["data"]:
                result[key] = action["data"][key]
        data = action["data"]
        if action["kind"] == "create":
            result["context"] = self._context(db, meta)
        elif "after" in data:
            changes = []
            before = data["before"]["records"]
            for identity, record in data["after"]["records"].items():
                if record is None:
                    changes.append({"id": identity, "removed": True})
                elif before.get(identity) is None:
                    changes.append(record)
                else:
                    changes.append(dict({"id": identity}, **{key: value for key, value in record.items()
                                    if before[identity].get(key) != value}))
            campaign_changes = copy.deepcopy(data["after"]["campaign"])
            if "scene_id" in campaign_changes:
                # Refresh destination context without repeating the fixed rules or unchanged
                # character sheet. Changed records already in this packet need no second delta.
                context = self._context(db, meta)
                for key in ("campaign", "rules", "character", "health_status", "inventory", "recent_beats", "pending", "last_action"):
                    context.pop(key, None)
                if "continuations" in context:
                    context["continuations"].pop("inventory", None)
                    if not context["continuations"]:
                        context.pop("continuations")
                        context.pop("context_incomplete", None)
                included = {record["id"] for value in context.values()
                            for record in (value if isinstance(value, list) else [value])
                            if isinstance(record, dict) and "id" in record}
                changes = [change for change in changes if change["id"] not in included]
                result["context"] = context
                campaign_changes.pop("scene_id")
            if changes or "context" not in result:
                result["changes"] = changes
            if campaign_changes:
                result["campaign_changes"] = campaign_changes
        if "replacement_action_id" in data:
            result["pending"] = self._summary(self._action(db, meta["id"], data["replacement_action_id"]))
        return result

    def _create(self, db, args):
        recovered = self._recover(db, "create", args)
        if recovered is not None:
            return recovered
        if db.execute("SELECT 1 FROM campaigns WHERE id=?", (args["campaign"],)).fetchone():
            _fail("campaign_exists", "This campaign already exists; resume it or choose a new identity.")
        meta = {"id": args["campaign"], "title": args["title"], "revision": 0,
                "scene_id": "opening", "character_id": "pc", "preferences": args["preferences"],
                "save_version": SAVE_VERSION, "rules_version": RULES["version"]}
        opening = args["opening"]
        initial = [normalize_character(args["character"]), default_record({"id": "opening", "kind": "scene",
                   "situation": opening["situation"], "place": opening.get("place"), "present": opening.get("present", [])})]
        initial.extend(normalize_possessions(args.get("possessions")))
        initial.extend(default_record(record) for record in opening.get("records", []))
        records = {}
        for record in initial:
            if record["id"] in records:
                _fail("duplicate_id", "Record identities must be distinct.", id=record["id"])
            records[record["id"]] = record
        self._validate_world(meta, records)
        db.execute("INSERT INTO campaigns VALUES(?,?)", (meta["id"], _json(meta)))
        self._save_world(db, meta, records, {})
        action = self._insert_action(db, meta["id"], "create", 0, {})
        self._request(db, "create", args, action["id"], 0)
        return self._receipt(db, meta, action, 0)

    def _act(self, db, args):
        recovered = self._recover(db, "act", args)
        if recovered is not None:
            return recovered
        meta = self._campaign(db, args["campaign"])
        self._revision(db, meta, args["expected_revision"])
        self._no_pending(db, meta["id"])
        records = self._records(db, meta["id"])
        old_meta, old_records = copy.deepcopy(meta), copy.deepcopy(records)
        self._changes(meta, records, args["changes"])
        meta["revision"] += 1
        data = self._images(old_meta, meta, old_records, records)
        if "beat" in args:
            data["beat"] = args["beat"]
        self._save_world(db, meta, records, old_records)
        action = self._insert_action(db, meta["id"], "act", meta["revision"], data)
        self._request(db, "act", args, action["id"], meta["revision"])
        return self._receipt(db, meta, action, meta["revision"])

    def _check(self, request, records, dice=None):
        return resolve_check(talent=request.get("talent"), difficulty=request.get("difficulty", RULES["default_difficulty"]),
                             character=records["pc"], dice=dice, rng=self.rng)

    def _begin(self, db, args):
        recovered = self._recover(db, "begin", args)
        if recovered is not None:
            return recovered
        meta = self._campaign(db, args["campaign"])
        self._revision(db, meta, args["expected_revision"])
        self._no_pending(db, meta["id"])
        records = self._records(db, meta["id"])
        check = self._check(args, records)
        meta["revision"] += 1
        data = {"intent": args["intent"], "stakes": args["stakes"], "check": check}
        action = self._insert_action(db, meta["id"], "begin", meta["revision"], data, "pending")
        db.execute("UPDATE campaigns SET data=? WHERE id=?", (_json(meta), meta["id"]))
        self._request(db, "begin", args, action["id"], meta["revision"])
        return self._receipt(db, meta, action, meta["revision"])

    def _close(self, db, command, args):
        meta = self._campaign(db, args["campaign"])
        action = self._action(db, meta["id"], args["action_id"])
        closing_hash = _hash([command, args])
        if action["closing_hash"] is not None:
            if action["closing_hash"] != closing_hash:
                _fail("request_conflict", "This action already has a different accepted closing request.")
            return self._receipt(db, meta, action, action["data"]["closed_revision"], True)
        if action["status"] != "pending":
            _fail("action_not_pending", "This action is no longer pending.", status=action["status"])
        self._revision(db, meta, action["created_revision"])
        records = self._records(db, meta["id"])
        old_meta, old_records = copy.deepcopy(meta), copy.deepcopy(records)
        if command == "commit":
            self._changes(meta, records, args["changes"])
            action["data"].update(self._images(old_meta, meta, old_records, records))
            if "beat" in args:
                action["data"]["beat"] = args["beat"]
            action["status"] = "completed"
        else:
            action["data"]["reason"] = args["reason"]
            action["status"] = "cancelled"
        meta["revision"] += 1
        action["accepted_revision"] = meta["revision"]
        action["closing_hash"], action["closing_kind"] = closing_hash, command
        action["data"]["closed_revision"] = meta["revision"]
        self._save_world(db, meta, records, old_records)
        self._update_action(db, action)
        return self._receipt(db, meta, action, meta["revision"])

    def _commit(self, db, args):
        return self._close(db, "commit", args)

    def _cancel(self, db, args):
        return self._close(db, "cancel", args)

    def _correct(self, db, args):
        recovered = self._recover(db, "correct", args)
        if recovered is not None:
            return recovered
        meta = self._campaign(db, args["campaign"])
        self._revision(db, meta, args["expected_revision"])
        target = self._action(db, meta["id"], args["target_action_id"])
        pending, latest = self._pending(db, meta["id"]), self._latest(db, meta["id"])
        if pending and pending["id"] != target["id"]:
            _fail("pending_action", "Correct or finish the current attempt first.", action_id=pending["id"])
        if target["kind"] not in ("act", "begin") or target["status"] not in ("pending", "completed") or latest["id"] != target["id"]:
            _fail("correction_not_allowed", "Only the current pending or latest completed gameplay action can be corrected.")
        records = self._records(db, meta["id"])
        old_meta, old_records = copy.deepcopy(meta), copy.deepcopy(records)
        if target["status"] == "completed":
            for identity, record in target["data"]["before"]["records"].items():
                if record is None:
                    records.pop(identity, None)
                else:
                    records[identity] = copy.deepcopy(record)
            meta.update(copy.deepcopy(target["data"]["before"]["campaign"]))
        self._validate_world(meta, records)
        meta["revision"] += 1
        target["status"] = "corrected"
        self._update_action(db, target)
        data = self._images(old_meta, meta, old_records, records)
        data.update(reason=args["reason"], target_action_id=target["id"])
        correction_id = _id()
        if "replacement_check" in args:
            request = args["replacement_check"]
            dice = target["data"].get("check", {}).get("dice")
            replacement = self._insert_action(db, meta["id"], "begin", meta["revision"],
                {"intent": request["intent"], "stakes": request["stakes"], "check": self._check(request, records, dice),
                 "replaces": target["id"], "correction_action_id": correction_id}, "pending")
            data["replacement_action_id"] = replacement["id"]
        action = self._insert_action(db, meta["id"], "correct", meta["revision"], data, action_id=correction_id)
        self._save_world(db, meta, records, old_records)
        self._request(db, "correct", args, action["id"], meta["revision"])
        return self._receipt(db, meta, action, meta["revision"])

    @staticmethod
    def _record(records, identity, kinds=None):
        if identity not in records:
            _fail("not_found", "Unknown record.", id=identity)
        record = records[identity]
        if kinds and record["kind"] not in kinds:
            _fail("invalid_reference", "Record has the wrong kind for this operation.",
                  id=identity, actual_kind=record["kind"], expected_kinds=list(kinds))
        return record

    def _validate_world(self, meta, records):
        validate_campaign(meta)
        for identity, record in records.items():
            validate_record(record)
            if identity != record["id"]:
                _fail("invalid_state", "Record identity does not match its key.")
        if [r["id"] for r in records.values() if r["kind"] == "character"] != ["pc"]:
            _fail("invalid_state", "Exactly one player character is required.")
        scene = self._record(records, meta["scene_id"], ("scene",))
        if scene["archived"]:
            _fail("invalid_reference", "The current scene cannot be retired.")
        for record in records.values():
            kind = record["kind"]
            if kind == "scene":
                if record["place"] is not None:
                    place = self._record(records, record["place"], ("place",))
                    if record["id"] == scene["id"] and place["archived"]:
                        _fail("invalid_reference", "The current place cannot be retired.")
                for identity in record["present"]:
                    present = self._record(records, identity, ("npc", "character"))
                    if record["id"] == scene["id"] and present.get("archived"):
                        _fail("invalid_reference", "A present entity cannot be retired.", id=identity)
            elif kind == "item":
                self._record(records, record["holder"], HOLDER_KINDS)
            elif kind in ("fact", "goal"):
                for identity in record["subjects"]:
                    self._record(records, identity)
                if kind == "fact" and record["source"] is not None:
                    self._record(records, record["source"])

    def _changes(self, meta, records, operations):
        preexisting = set(records)
        for operation in operations:
            if operation["op"] == "create_record":
                record = default_record(operation["record"])
                if record["id"] in records:
                    _fail("duplicate_id", "A record with this identity already exists.", id=record["id"])
                records[record["id"]] = record
        patched = set()
        for operation in operations:
            op = operation["op"]
            if op == "create_record":
                continue
            if op == "patch_record":
                identity = operation["id"]
                record = self._record(records, identity)
                for key in operation["changes"]:
                    if (identity, key) in patched:
                        _fail("conflicting_patch", "A field may be assigned only once in a transaction.", id=identity, field=key)
                    patched.add((identity, key))
                records[identity] = apply_patch(record, operation["changes"])
            elif op == "retire_record":
                self._record(records, operation["id"], ("npc", "place", "scene", "fact"))["archived"] = True
            elif op == "set_scene":
                self._record(records, operation["id"], ("scene",))
                if ("campaign", "scene_id") in patched:
                    _fail("conflicting_patch", "Set the scene once per transaction.")
                patched.add(("campaign", "scene_id"))
                meta["scene_id"] = operation["id"]
            elif op == "update_preferences":
                for key in operation["changes"]:
                    if ("preferences", key) in patched:
                        _fail("conflicting_patch", "A preference may be assigned only once.", field=key)
                    patched.add(("preferences", key))
                meta["preferences"].update(operation["changes"])
            elif op == "harm":
                records["pc"]["health"] = apply_harm(records["pc"]["health"], operation["severity"])
            elif op == "rest":
                records["pc"]["health"] = rest()
            elif op in ("use_item", "consume_item", "transfer_item"):
                item = self._record(records, operation["id"], ("item",))
                expected = operation.get("expected_holder", "pc")
                if item["holder"] != expected:
                    _fail("wrong_holder", "The item is not held by the expected owner.", id=item["id"], holder=item["holder"])
                amount = 1 if op == "use_item" else operation["quantity"]
                remaining = consume_stock(item["quantity"], amount)
                if op == "use_item":
                    if item["use"] != "heal":
                        _fail("invalid_rule", "This item has no restorative use.", id=item["id"])
                    records["pc"]["health"] = heal(records["pc"]["health"])
                    item["quantity"] = remaining
                elif op == "consume_item":
                    item["quantity"] = remaining
                else:
                    destination = self._record(records, operation["to"], HOLDER_KINDS)
                    if destination["id"] == expected:
                        _fail("invalid_transfer", "Transfer requires a different holder.")
                    if remaining == 0:
                        item["holder"] = destination["id"]
                    else:
                        item["quantity"] = remaining
                        split = copy.deepcopy(item)
                        split.update(id=_id("item"), holder=destination["id"], quantity=amount)
                        records[split["id"]] = split
            elif op in ("resolve_goal", "abandon_goal"):
                goal = self._record(records, operation["id"], ("goal",))
                if goal["status"] != "active":
                    _fail("invalid_goal", "Only an active goal can be resolved or abandoned.")
                goal["status"] = "resolved" if op == "resolve_goal" else "abandoned"
            elif op == "advance":
                goal = self._record(records, operation["id"], ("goal",))
                if goal["id"] not in preexisting or not goal["milestone"] or goal["status"] != "resolved" or goal["rewarded"]:
                    _fail("invalid_goal", "Advancement requires an unclaimed, resolved milestone that predates this action.")
                records["pc"]["talents"] = advance_talent(records["pc"]["talents"], operation["talent"])
                goal["rewarded"] = True
        self._validate_world(meta, records)

    @staticmethod
    def _visible(record, audience):
        return audience == "gm" or record.get("visibility", "player") == "player"

    def _project(self, record, records, audience):
        if record is None or not self._visible(record, audience):
            return None
        # Stored records are strictly typed; this explicit public whitelist prevents future
        # implementation fields from silently becoming player-visible.
        if audience == "gm":
            return copy.deepcopy(record)
        result = {key: copy.deepcopy(record[key]) for key in PUBLIC_FIELDS[record["kind"]]}
        visible = {key for key, value in records.items() if self._visible(value, audience)}
        for key in ("present", "subjects"):
            if key in result:
                result[key] = [identity for identity in result[key] if identity in visible]
        for key in ("source", "place", "holder"):
            if result.get(key) is not None and result[key] not in visible:
                result[key] = None
        return result

    @staticmethod
    def _context_records(meta, records):
        scene = records[meta["scene_id"]]
        sections = {
            "inventory": [r for r in records.values() if r["kind"] == "item" and r["holder"] == "pc" and r["quantity"] > 0],
            "nearby_items": [r for r in records.values() if r["kind"] == "item" and r["holder"] in (scene["id"], scene["place"]) and r["quantity"] > 0],
            "present": [records[identity] for identity in scene["present"]],
            "goals": [r for r in records.values() if r["kind"] == "goal" and (r["status"] == "active" or
                      (r["status"] == "resolved" and r["milestone"] and not r["rewarded"]))],
        }
        subjects = {"pc", scene["id"], scene["place"]} | {r["id"] for values in sections.values() for r in values}
        sections["facts"] = [r for r in records.values() if r["kind"] == "fact" and not r["archived"] and subjects.intersection(r["subjects"])]
        return sections

    @staticmethod
    def _cursor(meta, selector, audience, offset):
        return base64.urlsafe_b64encode(_json({"signature": _hash([meta["id"], selector, audience]),
                                             "revision": meta["revision"], "offset": offset}).encode()).decode()

    def _context(self, db, meta, audience="gm"):
        records = self._records(db, meta["id"])
        scene = records[meta["scene_id"]]
        project = lambda record: self._project(record, records, audience)
        sections = self._context_records(meta, records)
        campaign = copy.deepcopy(meta)
        if audience == "player" and not self._visible(scene, audience):
            campaign["scene_id"] = None
        latest = self._summary(self._latest(db, meta["id"]))
        last_status = {key: latest[key] for key in ("action_id", "kind", "status", "accepted_revision")} if latest else None
        result = {"campaign": campaign, "rules": rules_capsule(), "character": project(records["pc"]),
                  "health_status": health_label(records["pc"]["health"]),
                  "scene": project(scene), "place": project(records.get(scene["place"])),
                  "pending": self._summary(self._pending(db, meta["id"])) if audience == "gm" else None,
                  "last_action": last_status}
        continuations = {}
        for section, values in sections.items():
            values = [r for r in values if self._visible(r, audience)]
            selected, omitted = values[:20], values[20:]
            if section == "facts":
                result["facts"] = [project(r) for r in selected if r.get("visibility") == "player"]
                if audience == "gm":
                    result["gm_facts"] = [project(r) for r in selected if r.get("visibility") == "gm"]
            else:
                result[section] = [project(r) for r in selected]
            if omitted:
                selector = {"by": "context", "section": section, "limit": 20}
                continuation = {"selector": selector, "cursor": self._cursor(meta, selector, audience, 20)}
                if section == "facts":
                    result["omitted_facts"] = dict(continuation, count=len(omitted), ids=[r["id"] for r in omitted[:100]])
                else:
                    continuations[section] = dict(continuation, count=len(omitted))
        if continuations:
            result["context_incomplete"] = True
            result["continuations"] = continuations
        rows = db.execute("SELECT data FROM actions WHERE campaign=? AND status='completed' "
                          "ORDER BY accepted_revision DESC,rowid DESC", (meta["id"],))
        beats = []
        for row in rows:
            data = json.loads(row[0])
            if "beat" in data:
                beats.append(data["beat"])
                if len(beats) == 3:
                    break
        result["recent_beats"] = list(reversed(beats))
        return result

    def _resume(self, db, args):
        campaign = args.get("campaign")
        if campaign is None:
            rows = [json.loads(row[0]) for row in db.execute("SELECT data FROM campaigns ORDER BY id")]
            if len(rows) == 1:
                campaign = rows[0]["id"]
        if campaign is None:
            result = {"campaigns": [{key: row[key] for key in ("id", "title", "revision")} for row in rows]}
        else:
            result = self._context(db, self._campaign(db, campaign), args.get("audience", "gm"))
        if args.get("include_guide", SCHEMAS["resume"]["properties"]["include_guide"]["default"]):
            from .guide import load_guide
            result["guide"] = load_guide()
        return result

    def _lookup(self, db, args):
        meta = self._campaign(db, args["campaign"])
        selector = args["selector"]
        audience = args.get("audience", "gm")
        result = {"campaign": meta["id"], "revision": meta["revision"]}
        if selector["by"] == "status":
            if "cursor" in args:
                _fail("invalid_cursor", "Status lookup does not paginate.")
            identity = selector.get("action_id")
            acceptance = None
            if identity is None:
                row = db.execute("SELECT action_id,accepted_revision FROM requests WHERE campaign=? AND id=?",
                                 (meta["id"], selector["request_id"])).fetchone()
                if row is None:
                    _fail("not_found", "Unknown request.")
                identity, acceptance = row
            action = self._action(db, meta["id"], identity)
            result["action"] = self._summary(action) if audience == "gm" else {key: self._summary(action)[key]
                                                                                 for key in ("action_id", "kind", "status", "accepted_revision")}
            if acceptance is not None:
                result["request_accepted_revision"] = acceptance
            return result
        offset = 0
        signature = _hash([meta["id"], selector, audience])
        if "cursor" in args:
            try:
                cursor = json.loads(base64.urlsafe_b64decode(args["cursor"].encode()))
                if type(cursor) is not dict or cursor["signature"] != signature or type(cursor["offset"]) is not int or cursor["offset"] < 0:
                    raise ValueError()
                if cursor["revision"] != meta["revision"]:
                    _fail("stale_cursor", "State changed; restart the lookup.", current_revision=meta["revision"])
                offset = cursor["offset"]
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                raise GameError("invalid_cursor", "This cursor does not belong to the query.") from exc
        limit = selector.get("limit", 20)
        records = self._records(db, meta["id"])
        if selector["by"] == "history":
            total = db.execute("SELECT count(*) FROM actions WHERE campaign=?", (meta["id"],)).fetchone()[0]
            actions = [self._action_row(row) for row in db.execute("SELECT * FROM actions WHERE campaign=? "
                       "ORDER BY created_revision,rowid LIMIT ? OFFSET ?", (meta["id"], limit, offset))]
            values = [dict(self._summary(action), **{key: action["data"][key] for key in ("before", "after")
                                                      if key in action["data"]}) for action in actions]
            if audience == "player":
                values = [{key: value[key] for key in ("action_id", "kind", "status", "accepted_revision", "beat") if key in value}
                          for value in values]
            field = "actions"
        else:
            values = list(records.values())
            by = selector["by"]
            if by == "ids":
                unknown = set(selector["ids"]) - records.keys()
                if unknown:
                    _fail("not_found", "Unknown record identities.", ids=sorted(unknown))
                values = [r for r in values if r["id"] in selector["ids"]]
            elif by == "subjects":
                values = [r for r in values if r["kind"] == "fact" and not r["archived"] and set(selector["subjects"]).intersection(r["subjects"])]
            elif by == "holder":
                values = [r for r in values if r["kind"] == "item" and r["holder"] == selector["holder"] and r["quantity"] > 0]
            elif by == "context":
                values = self._context_records(meta, records)[selector["section"]]
            elif by == "search":
                query = selector["query"].casefold()
                values = [r for r in values if query in " ".join(str(r.get(key, "")) for key in
                          ("id", "name", "text", "situation", "concept", "motivation")).casefold() and not r.get("archived")]
            values = [self._project(r, records, audience) for r in values if self._visible(r, audience)]
            field = "records"
        if selector["by"] == "history":
            result[field] = values
        else:
            result[field] = values[offset:offset + limit]
            total = len(values)
        result["total"] = total
        if offset + limit < total:
            result["cursor"] = self._cursor(meta, selector, audience, offset + limit)
        return result

    def backup(self):
        directory = self.data_dir / "backups"
        directory.mkdir(exist_ok=True)
        destination = directory / ("collection-" + _id("backup") + ".sqlite")
        temporary = destination.with_suffix(".tmp")
        try:
            with closing(self._connect()) as source, closing(sqlite3.connect(str(temporary))) as target:
                source.backup(target)
            os.replace(temporary, destination)
            backups = []
            for path in directory.glob("collection-*.sqlite"):
                try:
                    backups.append((path.stat().st_mtime_ns, path))
                except FileNotFoundError:
                    pass
            for _, old in sorted(backups, reverse=True)[5:]:
                old.unlink(missing_ok=True)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    def _validate_journal(self, db, meta):
        """Validate saved recovery links and shapes without replaying game mechanics."""
        def require(condition):
            if not condition:
                _fail("invalid_backup", "Backup action history or retry identities are invalid.")

        def digest(value):
            return (type(value) is str and len(value) == 64
                    and all(char in "0123456789abcdef" for char in value))

        def images(data):
            for side in ("before", "after"):
                value = data[side]
                require(type(value) is dict and set(value) == {"records", "campaign"})
                require(type(value["records"]) is dict and type(value["campaign"]) is dict)
                for identity, record in value["records"].items():
                    validate_value(identity, ID_SCHEMA)
                    if record is not None:
                        validate_record(record)
                        require(record["id"] == identity)
                    else:
                        require(identity != "pc")
                # These are the campaign fields that gameplay can change.
                require(set(value["campaign"]) <= {"scene_id", "preferences"})
                for key, field in value["campaign"].items():
                    validate_value(field, CAMPAIGN_SCHEMA["properties"][key])
            require(data["before"]["records"].keys() == data["after"]["records"].keys())
            require(data["before"]["campaign"].keys() == data["after"]["campaign"].keys())

        rows = list(db.execute("SELECT * FROM actions WHERE campaign=?", (meta["id"],)))
        actions = {}
        revisions = set()
        pending = []
        for row in rows:
            action = self._action_row(row)
            identity, kind, status, data = action["id"], action["kind"], action["status"], action["data"]
            validate_value(identity, ID_SCHEMA)
            require(identity not in actions)
            actions[identity] = action
            validate_json(data)
            require(type(data) is dict)
            require(kind in ("create", "act", "begin", "correct"))
            require(status in ({"completed", "corrected"} if kind == "act" else
                               {"pending", "completed", "cancelled", "corrected"} if kind == "begin" else
                               {"completed"}))
            for key in ("created_revision", "accepted_revision"):
                require(type(action[key]) is int and 0 <= action[key] <= meta["revision"])
                revisions.add(action[key])
            require(action["created_revision"] <= action["accepted_revision"])
            required, optional = set(), set()
            if kind == "create":
                require(action["created_revision"] == action["accepted_revision"] == 0)
            else:
                require(action["created_revision"] > 0)
            if kind in ("act", "correct"):
                required.update(("before", "after"))
                require(action["created_revision"] == action["accepted_revision"])
            if kind == "act":
                optional.add("beat")
            if kind == "correct":
                required.update(("reason", "target_action_id"))
                optional.add("replacement_action_id")
            if kind == "begin":
                required.update(("intent", "stakes", "check"))
                optional.update(("replaces", "correction_action_id"))
                require(("replaces" in data) == ("correction_action_id" in data))
                check = data["check"]
                require(type(check) is dict)
                talent = check["talent"]
                validate_value(talent, CHECK_FIELDS["talent"])
                character = {"talents": {} if talent is None else {talent: check["modifier"]}}
                expected = resolve_check(talent, check["difficulty"], character, dice=check["dice"])
                require(_json(check) == _json(expected))
                if status == "pending":
                    pending.append(action)
                    require(action["accepted_revision"] == meta["revision"])
            if action["closing_hash"] is not None:
                require(kind == "begin" and digest(action["closing_hash"]))
                require(action["closing_kind"] in ("commit", "cancel"))
                required.add("closed_revision")
                require(type(data["closed_revision"]) is int and
                        data["closed_revision"] == action["accepted_revision"] == action["created_revision"] + 1)
                if action["closing_kind"] == "commit":
                    require(status in ("completed", "corrected"))
                    required.update(("before", "after"))
                    optional.add("beat")
                else:
                    require(status == "cancelled")
                    required.add("reason")
            else:
                require(action["closing_kind"] is None)
                require(kind != "begin" or status in ("pending", "corrected"))
                require(action["created_revision"] == action["accepted_revision"])
            require(required <= set(data) <= required | optional)
            for key in ("intent", "stakes", "beat", "reason"):
                if key in data:
                    validate_value(data[key], SHORT)
            for key in ("replaces", "correction_action_id", "target_action_id", "replacement_action_id"):
                if key in data:
                    validate_value(data[key], ID_SCHEMA)
            if "before" in data:
                images(data)
        require(len(pending) <= 1)
        require(sum(action["kind"] == "create" for action in actions.values()) == 1)
        # Revisions are bounded above, so cardinality detects gaps without allocating
        # a range based on an untrusted revision number.
        require(len(revisions) == meta["revision"] + 1)
        events = {}
        for action in actions.values():
            if "replaces" not in action["data"]:
                events.setdefault(action["created_revision"], []).append(action["id"])
            if action["closing_hash"] is not None:
                events.setdefault(action["accepted_revision"], []).append(action["id"])
        require(len(events) == meta["revision"] + 1 and all(len(values) == 1 for values in events.values()))
        records = self._records(db, meta["id"])
        for action in actions.values():
            if action["accepted_revision"] == meta["revision"] and "after" in action["data"]:
                after = action["data"]["after"]
                require(all(records.get(key) == value for key, value in after["records"].items()))
                require(all(meta[key] == value for key, value in after["campaign"].items()))
        for action in pending:
            check = action["data"]["check"]
            require(_json(check) == _json(self._check(check, records, check["dice"])))

        corrected = set()
        for action in actions.values():
            data = action["data"]
            if action["kind"] == "correct":
                target = actions.get(data["target_action_id"])
                require(target is not None and target["kind"] in ("act", "begin") and
                        target["status"] == "corrected" and target["id"] not in corrected)
                corrected.add(target["id"])
                require(target["accepted_revision"] + 1 == action["created_revision"])
                empty = {"records": {}, "campaign": {}}
                require(data["before"] == target["data"].get("after", empty) and
                        data["after"] == target["data"].get("before", empty))
                if "replacement_action_id" in data:
                    replacement = actions.get(data["replacement_action_id"])
                    require(replacement is not None and replacement["kind"] == "begin" and
                            replacement["created_revision"] == action["created_revision"] and
                            replacement["data"].get("correction_action_id") == action["id"] and
                            replacement["data"].get("replaces") == target["id"])
                    if "check" in target["data"]:
                        require(replacement["data"]["check"]["dice"] == target["data"]["check"]["dice"])
            if "replaces" in data:
                correction = actions.get(data["correction_action_id"])
                require(correction is not None and correction["kind"] == "correct" and
                        correction["data"].get("replacement_action_id") == action["id"])
        require(corrected == {a["id"] for a in actions.values() if a["status"] == "corrected"})
        requested = set()
        for row in db.execute("SELECT * FROM requests WHERE campaign=?", (meta["id"],)):
            validate_value(row["id"], REQUEST)
            require(digest(row["payload_hash"]))
            action = actions.get(row["action_id"])
            require(action is not None and action["id"] not in requested and
                    row["command"] == action["kind"] and "replaces" not in action["data"] and
                    type(row["accepted_revision"]) is int and row["accepted_revision"] == action["created_revision"])
            requested.add(action["id"])
        require(requested == {a["id"] for a in actions.values() if "replaces" not in a["data"]})

    def restore(self, path):
        """Maintenance only: all callers/writers must be stopped before replacement."""
        source_path = Path(path).expanduser().resolve()
        if source_path == self.db_path or not source_path.is_file():
            _fail("invalid_backup", "Choose an existing collection backup, not the live database.")
        handle, temporary_name = tempfile.mkstemp(prefix=".restore-", suffix=".sqlite", dir=self.data_dir)
        os.close(handle)
        temporary = Path(temporary_name)
        try:
            with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source:
                if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or source.execute("PRAGMA user_version").fetchone()[0] != SAVE_VERSION:
                    _fail("invalid_backup", "Backup integrity or format validation failed.")
                self._validate_structure(source)
                if source.execute("PRAGMA foreign_key_check").fetchone():
                    _fail("invalid_backup", "Backup schema or references are invalid.")
                source.row_factory = sqlite3.Row
                for row in source.execute("SELECT id,data FROM campaigns"):
                    meta = json.loads(row[1])
                    if type(meta) is not dict or meta.get("id") != row[0]:
                        _fail("invalid_backup", "Backup campaign identity does not match its key.")
                    self._validate_world(meta, self._records(source, meta["id"]))
                    self._validate_journal(source, meta)
                with closing(sqlite3.connect(str(temporary))) as target:
                    source.backup(target)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            # Recover/checkpoint the old file before discarding its sidecars, so
            # it remains usable if replacement fails. All callers must be stopped.
            sidecars = [Path(str(self.db_path) + suffix) for suffix in ("-journal", "-wal", "-shm")]
            if self.db_path.exists() and any(path.exists() for path in sidecars):
                try:
                    with closing(sqlite3.connect(str(self.db_path), isolation_level=None)) as current:
                        current.execute("SELECT count(*) FROM sqlite_master").fetchone()
                        if current.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                            _fail("restore_failed", "Stop all database callers before restoring.")
                        if current.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                            _fail("restore_failed", "Stop all database callers before restoring.")
                except sqlite3.Error as exc:
                    raise GameError("restore_failed", "Existing SQLite recovery files could not be settled. "
                        "Stop all callers; if the old collection is damaged, preserve its database "
                        "and sidecars together before restoring.") from exc
            for sidecar in sidecars:
                sidecar.unlink(missing_ok=True)
            os.replace(temporary, self.db_path)
            return {"restored": str(self.db_path), "refresh_required": True,
                    "message": "Recovery boundary: discard pre-restore context and resume saved play."}
        except (sqlite3.Error, ValueError, KeyError, TypeError, RecursionError, GameError) as exc:
            if isinstance(exc, GameError) and exc.code == "restore_failed":
                raise
            raise GameError("invalid_backup", "Backup validation failed.") from exc
        finally:
            temporary.unlink(missing_ok=True)
