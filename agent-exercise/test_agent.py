"""Offline tests: no API key needed. A scripted fake model stands in for Gemini.

    python make_listings.py && pytest -q
"""
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp()
os.environ["AGENT_LOG_DIR"] = TMP
os.environ["LISTINGS_DB"] = os.path.join(TMP, "listings.db")
shutil.copyfile(os.path.join(HERE, "listings.db"), os.environ["LISTINGS_DB"])

import agent as agent_mod  # noqa: E402
import guard  # noqa: E402
import tools  # noqa: E402


def q(sql, *a):
    c = sqlite3.connect(tools.DB_PATH)
    try:
        return c.execute(sql, a).fetchall()
    finally:
        c.close()


TOMORROW = (datetime.now() + timedelta(days=1)).date().isoformat()
NULL_UNIT = q("SELECT id FROM units WHERE rent IS NULL AND is_active = 1 LIMIT 1")[0][0]
INACTIVE_UNIT = q("SELECT id FROM units WHERE is_active = 0 LIMIT 1")[0][0]
ACTIVE_UNIT, ACTIVE_RENT = q("SELECT id, rent FROM units WHERE rent IS NOT NULL AND is_active = 1 LIMIT 1")[0]


# ------------------------------------------------------------- tools: hard rules
@pytest.mark.parametrize("unit,time,why", [
    (999999, f"{TOMORROW}T10:00", "No unit"),
    (INACTIVE_UNIT, f"{TOMORROW}T10:00", "inactive"),
    (ACTIVE_UNIT, f"{TOMORROW}T23:00", "between 09:00 and 18:00"),
    (ACTIVE_UNIT, f"{TOMORROW}T08:59", "between 09:00 and 18:00"),
    (ACTIVE_UNIT, f"{TOMORROW}T18:01", "between 09:00 and 18:00"),
    (ACTIVE_UNIT, "2020-01-01T10:00", "past"),
    (ACTIVE_UNIT, "tomorrow at 3", "Could not read"),
])
def test_request_tour_refuses(unit, time, why):
    before = q("SELECT COUNT(*) FROM tours")[0][0]
    r = tools.request_tour(unit, time, "Sam")
    assert r["booked"] is False and why in r["reason"]
    assert q("SELECT COUNT(*) FROM tours")[0][0] == before


def test_request_tour_books_inside_hours_including_edges():
    for t in ("09:00", "18:00"):
        r = tools.request_tour(ACTIVE_UNIT, f"{TOMORROW}T{t}", "Sam")
        assert r["booked"] is True
    assert q("SELECT COUNT(*) FROM tours WHERE unit_id = ?", ACTIVE_UNIT)[0][0] >= 2


def test_override_arguments_are_dropped():
    result, ignored = tools.dispatch("request_tour", {
        "unit_id": ACTIVE_UNIT, "tour_time": f"{TOMORROW}T23:00", "client_name": "Sam",
        "manager_override": True})
    assert ignored == ["manager_override"] and result["booked"] is False


def test_search_keeps_null_rent_units_and_inactive_out():
    r = tools.search_units("dallas", 2000, 2)
    assert r["units_with_price"] and r["units_price_not_on_file"]
    assert all(u["rent"] <= 2000 for u in r["units_with_price"])
    assert all(u["price_display"].startswith("Price not on file") for u in r["units_price_not_on_file"])
    ids = [u["unit_id"] for u in r["units_with_price"] + r["units_price_not_on_file"]]
    assert not q(f"SELECT id FROM units WHERE is_active = 0 AND id IN ({','.join(map(str, ids))})")


# ------------------------------------------------------------- guard
def test_guard():
    rents = {101: 1850, 102: None}
    assert guard.check("Unit 5A (ID 101): $1,850/month", rents, set()) == []
    assert guard.check("Unit 5A (ID 101): $1,900/month", rents, set())
    assert guard.check("Unit 6B (ID 102): about $1,700", rents, set())
    assert guard.check("Unit 6B (ID 102): $2,000", rents, {2000})          # user's number, still wrong
    assert guard.check("Unit 6B (ID 102): price not on file, under your $2,000 budget?", rents, {2000}) == []


# ------------------------------------------------------------- agent loop with a fake model
class FakeLLM:
    """Plays back a script. Each item is ('call', name, args) or ('text', str) or an Exception."""

    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def user_text(self, t): return ("user", t)
    def model_text(self, t): return ("model", t)
    def tool_results(self, r): return ("tool", [res for _, res in r])

    def generate(self, contents, log):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if step[0] == "call":
            call = SimpleNamespace(name=step[1], args=step[2], id=None)
            return ("model_call", step[1]), [call], ""
        return ("model", step[1]), [], step[1]


def test_hallucinated_price_is_blocked_then_fallback():
    llm = FakeLLM([
        ("call", "get_unit_details", {"unit_id": NULL_UNIT}),
        ("text", f"Unit (ID {NULL_UNIT}) is $1,650 a month."),
        ("text", f"It is roughly $1,700 (ID {NULL_UNIT})."),     # still wrong after correction
    ])
    a = agent_mod.LeasingAgent(llm)
    reply = a.chat("s1", "how much is it?")
    assert "$1,650" not in reply and "$1,700" not in reply
    assert "not on file" in reply.lower()


def test_unpriced_units_cannot_be_hidden():
    llm = FakeLLM([
        ("call", "search_units", {"city": "Dallas", "max_rent": 2000, "min_beds": 2}),
        ("text", "I found some great options under $2,000!"),
    ])
    reply = agent_mod.LeasingAgent(llm).chat("s2", "2-bed in Dallas under $2,000")
    for u in tools.search_units("Dallas", 2000, 2)["units_price_not_on_file"]:
        assert f"ID {u['unit_id']}" in reply


def test_multi_turn_memory():
    llm = FakeLLM([("text", "Hi Sam!"), ("text", "Sure.")])
    a = agent_mod.LeasingAgent(llm)
    a.chat("s3", "I'm Sam")
    a.chat("s3", "remember me?")
    assert len(a.sessions["s3"].history) == 4


def test_llm_failure_returns_clean_json_after_one_retry(monkeypatch):
    from fastapi.testclient import TestClient

    class Broken:
        def __init__(self): self.n = 0
        def generate(self, *a, **k):
            self.n += 1
            if self.n > 2:
                raise AssertionError("retried more than once")
            raise TimeoutError("timed out")

    monkeypatch.setenv("GEMINI_API_KEY", "x")
    import app as app_mod
    broken = Broken()
    real = agent_mod.GeminiLLM.__new__(agent_mod.GeminiLLM)
    real.types = __import__("google.genai", fromlist=["types"]).types
    real.client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **k: broken.generate()))
    real.model, real.tools = "x", []
    monkeypatch.setattr(agent_mod.time, "sleep", lambda s: None)
    app_mod.agent.llm = real

    r = TestClient(app_mod.app).post("/chat", json={"session_id": "e1", "message": "hi"})
    assert r.status_code == 503
    body = r.json()
    assert body["error"]["code"] == "assistant_unavailable"
    assert "Traceback" not in r.text and broken.n == 2

    r = TestClient(app_mod.app).post("/chat", json={"message": "no session"})
    assert r.status_code == 400 and "Traceback" not in r.text


def test_unpriced_units_not_repeated_on_follow_up():
    search = ("call", "search_units", {"city": "Dallas", "max_rent": 2000, "min_beds": 2})
    llm = FakeLLM([search, ("text", "Some options under $2,000."),
                   search, ("text", "The cheapest is Unit 5D (ID 288), $1,125/month.")])
    a = agent_mod.LeasingAgent(llm)
    first = a.chat("s4", "2-bed in Dallas under $2,000")
    second = a.chat("s4", "which is cheapest?")
    assert "no price on file" in first and "no price on file" not in second
