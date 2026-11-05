import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .models import digest, now


SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
 id TEXT PRIMARY KEY, input_hash TEXT NOT NULL UNIQUE, raw TEXT NOT NULL,
 source TEXT NOT NULL, step TEXT NOT NULL, status TEXT NOT NULL,
 state TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, provider_id TEXT NOT NULL,
 profile TEXT NOT NULL, UNIQUE(kind, provider_id)
);
CREATE TABLE IF NOT EXISTS aliases (
 entity_id TEXT NOT NULL, alias TEXT NOT NULL, evidence_id TEXT NOT NULL,
 PRIMARY KEY(entity_id, alias)
);
CREATE TABLE IF NOT EXISTS cache (
 key TEXT PRIMARY KEY, value TEXT NOT NULL, retrieved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
 scope TEXT PRIMARY KEY, next_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
 key TEXT PRIMARY KEY, case_id TEXT NOT NULL, kind TEXT NOT NULL,
 payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
 status TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocations (
 obligation_id TEXT PRIMARY KEY, action_key TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, case_id TEXT NOT NULL, at TEXT NOT NULL, message TEXT NOT NULL
);
"""


class Conflict(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def ingest(self, raw, source):
        fingerprint = digest(raw.strip())
        cid = "CASE-" + fingerprint[:20]
        with self.connect() as c:
            c.execute("INSERT OR IGNORE INTO cases VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (cid, fingerprint, raw, source, "extract", "RUNNING", "{}", None, now(), now()))
        return cid

    def get(self, cid):
        with self.connect() as c:
            row = c.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone()
        if not row:
            raise KeyError(cid)
        result = dict(row)
        result["state"] = json.loads(result["state"])
        return result

    def save(self, cid, step, state, status="RUNNING", error=None):
        with self.connect() as c:
            c.execute("UPDATE cases SET step=?,state=?,status=?,error=?,updated_at=? WHERE id=?",
                      (step, json.dumps(state), status, error, now(), cid))

    def event(self, cid, message):
        with self.connect() as c:
            c.execute("INSERT INTO events(case_id,at,message) VALUES (?,?,?)", (cid, now(), message))

    @staticmethod
    def can_retry_extraction(case):
        return (case["status"] == "HOLD" and case["step"] == "done"
                and not case["state"].get("extraction") and not case["state"].get("action_key"))

    def reopen_extraction(self, cid, mode):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM cases WHERE id=?", (cid,)).fetchone()
            if row is None:
                raise KeyError(cid)
            case = dict(row)
            case["state"] = json.loads(case["state"])
            if (not self.can_retry_extraction(case)
                    or case["state"].get("mode", mode) != mode
                    or c.execute("SELECT 1 FROM actions WHERE case_id=?", (cid,)).fetchone()):
                raise Conflict("Only a failed document read without actions can be retried")
            state = {"mode": mode, "read_attempt": case["state"].get("read_attempt", 1) + 1}
            c.execute("UPDATE cases SET step='extract',status='RUNNING',state=?,error=NULL,updated_at=? WHERE id=?",
                      (json.dumps(state), now(), cid))
            c.execute("INSERT INTO events(case_id,at,message) VALUES (?,?,?)",
                      (cid, now(), "Retrying document reading; prior history retained"))
        return cid

    def entity(self, kind, provider_id, profile, alias, evidence_id):
        eid = f"{kind}:{provider_id}"
        with self.connect() as c:
            c.execute("INSERT INTO entities VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET profile=excluded.profile",
                      (eid, kind, str(provider_id), json.dumps(profile)))
            c.execute("INSERT OR IGNORE INTO aliases VALUES (?,?,?)", (eid, alias, evidence_id))
        return eid

    def reserve(self, cid, kind, key, payload, obligation_id=None):
        fingerprint = digest(payload)
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute("SELECT * FROM actions WHERE key=?", (key,)).fetchone()
            if existing:
                if existing["payload_hash"] != fingerprint:
                    raise Conflict("Same action identity with changed immutable details")
                return dict(existing)
            if obligation_id:
                used = c.execute("SELECT action_key FROM allocations WHERE obligation_id=?", (obligation_id,)).fetchone()
                if used and used[0] != key:
                    raise Conflict("This approved obligation/receipt is already reserved")
                c.execute("INSERT INTO allocations VALUES (?,?)", (obligation_id, key))
            c.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?)",
                      (key, cid, kind, json.dumps(payload), fingerprint, "pending", None, now()))
            return dict(c.execute("SELECT * FROM actions WHERE key=?", (key,)).fetchone())

    def complete(self, key, result):
        with self.connect() as c:
            c.execute("UPDATE actions SET status='complete',result=? WHERE key=?", (json.dumps(result), key))

    def audit(self, cid):
        case = self.get(cid)
        with self.connect() as c:
            case["events"] = [dict(r) for r in c.execute("SELECT * FROM events WHERE case_id=? ORDER BY id", (cid,))]
            # A replay case can point at an action originally created by another case.
            key = case["state"].get("action_key", "")
            case["actions"] = [dict(r) for r in c.execute("SELECT * FROM actions WHERE case_id=? OR key=?", (cid, key))]
        return case
