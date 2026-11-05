"""Isolated, offline test setup.

Every test gets its own temporary databases and copies of the registers. Crustdata
answers come only from the cached responses in tests/crustdata_cache.json (recorded
from live calls on 2026-11-05); no live Crustdata or Claude calls are made.
"""

import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
OFFLINE_ENV = {
    "MUNIM_OFFLINE": "1",
    "MUNIM_NO_LLM": "1",
    "MUNIM_CACHE_MAX_AGE_HOURS": "1000000",
    "MUNIM_TEAM_EMAILS": "ap-lead@munim.local,ops-lead@munim.local",
}
os.environ.update(OFFLINE_ENV)
sys.path.insert(0, str(ROOT))

from munim import config, db  # noqa: E402

CACHE_FILE = ROOT / "tests" / "crustdata_cache.json"


def seed_cache(conn):
    with conn:
        for row in json.loads(CACHE_FILE.read_text(encoding="utf-8")):
            conn.execute(
                "INSERT OR REPLACE INTO api_cache (key, endpoint, request, response, credits, retrieved_at) "
                "VALUES (?,?,?,?,?,?)",
                (row["key"], row["endpoint"], row["request"], row["response"], row["credits"], row["retrieved_at"]))


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = {
        "DB_PATH": tmp_path / "munim.db",
        "BANK_DB_PATH": tmp_path / "bank.db",
        "VENDOR_REGISTER": tmp_path / "vendors.json",
        "PURCHASE_ORDERS": tmp_path / "purchase_orders.json",
    }
    shutil.copy(config.DATA_DIR / "vendors.json", paths["VENDOR_REGISTER"])
    shutil.copy(config.DATA_DIR / "purchase_orders.json", paths["PURCHASE_ORDERS"])
    for name, value in paths.items():
        monkeypatch.setattr(config, name, value)
    conn = db.connect()
    seed_cache(conn)
    yield SimpleNamespace(conn=conn, tmp=tmp_path, **{k.lower(): v for k, v in paths.items()})
    conn.close()


@pytest.fixture
def subprocess_env(env):
    """Environment for running `python -m munim` in a separate process against the same temp data."""
    e = dict(os.environ)
    e.update(OFFLINE_ENV)
    e.update({
        "MUNIM_DB": str(env.db_path), "MUNIM_BANK_DB": str(env.bank_db_path),
        "MUNIM_VENDORS": str(env.vendor_register), "MUNIM_POS": str(env.purchase_orders),
        "PYTHONIOENCODING": "utf-8",
    })
    e.pop("ANTHROPIC_API_KEY", None)
    return e
