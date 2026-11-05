"""Sandbox sink: commits the effect independently of workflow acknowledgement."""
import json
import sqlite3
from pathlib import Path

from .models import digest, now
from .store import Conflict


class Sandbox:
    def __init__(self, path: Path, team):
        self.path, self.team = Path(path), frozenset(team)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS effects (key TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL, reference TEXT NOT NULL, at TEXT NOT NULL)")

    def execute(self, key, kind, payload):
        if kind == "team_draft":
            if payload.get("to") not in self.team:
                raise Conflict("Only configured team recipients may receive sandbox drafts")
        elif kind == "sandbox_payment":
            if payload.get("currency") != "INR" or not isinstance(payload.get("amount_minor"), int) or payload["amount_minor"] <= 0:
                raise Conflict("Unsupported payment payload")
        else:
            raise Conflict("Unknown action kind")
        with sqlite3.connect(self.path, timeout=30) as c:
            c.execute("BEGIN IMMEDIATE")
            existing = c.execute("SELECT hash,reference FROM effects WHERE key=?", (key,)).fetchone()
            if existing:
                if existing[0] != digest([kind, payload]):
                    raise Conflict("Sink rejected different details under the same idempotency key")
                return {"reference": existing[1], "sandbox": True}
            reference = "SIM-" + digest(key)[:16]
            c.execute("INSERT INTO effects VALUES (?,?,?,?,?,?)",
                      (key, kind, json.dumps(payload), digest([kind, payload]), reference, now()))
            return {"reference": reference, "sandbox": True}

    def list(self):
        with sqlite3.connect(self.path) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM effects ORDER BY at,key")]
