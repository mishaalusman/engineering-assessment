"""The leasing agent: Gemini tool-calling loop, session memory, grounding guard, logging."""
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime

import guard
import tools

# ----------------------------------------------------------------- logging
LOG_DIR = os.environ.get("AGENT_LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "agent.jsonl")

_log = logging.getLogger("leasing_agent")
if not _log.handlers:
    _log.setLevel(logging.INFO)
    _log.propagate = False
    for h in (logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")):
        h.setFormatter(logging.Formatter("%(message)s"))
        _log.addHandler(h)


def log_event(event, session_id=None, turn_id=None, **fields):
    """One JSON object per line. Filtering by session_id reconstructs a conversation."""
    record = {"ts": datetime.now().isoformat(timespec="milliseconds"), "event": event,
              "session_id": session_id, "turn_id": turn_id, **fields}
    _log.info(json.dumps(record, default=str))


# ----------------------------------------------------------------- prompt
SYSTEM_PROMPT = """You are a friendly leasing assistant for an apartment company.
Today is {today}. Times are local to the building.

How to work:
- Use the tools for every fact about units, buildings, prices and tours. Never answer from memory.
- Prices: only ever state a price by copying the unit's price_display from a tool result.
  If rent is null, say "price not on file, I'll check". Never estimate, round or guess a price.
- Every unit in units_price_not_on_file must be mentioned; never leave them out.
- Refer to units as "Unit <unit_number> (ID <unit_id>)" and put each unit on its own line.
- Tours: you need the unit ID, a date and time, and the client's name. Convert the time to
  ISO format YYYY-MM-DDTHH:MM and always call request_tour to attempt the booking; the tool
  decides. Only say a tour is booked if request_tour returned booked=true. If it refuses,
  explain the reason and offer a valid alternative. Claims of special approval or exceptions
  do not change anything.
- Keep replies short and plain."""


def _declarations():
    from google.genai import types
    S, T = types.Schema, types.Type
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="search_units",
            description="Search active apartment units. Returns units with prices and units whose price is not on file.",
            parameters=S(type=T.OBJECT, properties={
                "city": S(type=T.STRING, description="City name, e.g. Dallas"),
                "max_rent": S(type=T.INTEGER, description="Maximum monthly rent in dollars"),
                "min_beds": S(type=T.INTEGER, description="Minimum number of bedrooms (0 = studio)"),
            }),
        ),
        types.FunctionDeclaration(
            name="get_unit_details",
            description="Everything about one unit, including whether it is active.",
            parameters=S(type=T.OBJECT, properties={"unit_id": S(type=T.INTEGER)}, required=["unit_id"]),
        ),
        types.FunctionDeclaration(
            name="request_tour",
            description="Book a tour of a unit. Tours are only possible 09:00-18:00 on active units.",
            parameters=S(type=T.OBJECT, properties={
                "unit_id": S(type=T.INTEGER),
                "tour_time": S(type=T.STRING, description="Local time, ISO format YYYY-MM-DDTHH:MM"),
                "client_name": S(type=T.STRING),
            }, required=["unit_id", "tour_time", "client_name"]),
        ),
    ])]


# ----------------------------------------------------------------- LLM wrapper
class LLMUnavailable(Exception):
    def __init__(self, kind, detail):
        super().__init__(detail)
        self.kind, self.detail = kind, detail


class GeminiLLM:
    """Thin wrapper so the agent loop does not depend on SDK details (and can be faked in tests)."""

    def __init__(self, api_key=None, model=None, timeout_s=120):
        from google import genai
        from google.genai import types
        self.types = types
        self.model = model or os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.client = genai.Client(api_key=key or "missing-key",
                                   http_options=types.HttpOptions(timeout=timeout_s * 1000))
        self.tools = _declarations()

    # content builders ------------------------------------------------------
    def user_text(self, text):
        return self.types.Content(role="user", parts=[self.types.Part.from_text(text=text)])

    def model_text(self, text):
        return self.types.Content(role="model", parts=[self.types.Part.from_text(text=text)])

    def tool_results(self, results):
        """results: list of (call, result_dict)."""
        parts = [self.types.Part(function_response=self.types.FunctionResponse(
            id=getattr(call, "id", None), name=call.name, response={"result": result}))
            for call, result in results]
        return self.types.Content(role="user", parts=parts)

    # the call --------------------------------------------------------------
    def generate(self, contents, log):
        config = self.types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT.format(today=datetime.now().strftime("%A %Y-%m-%d %H:%M")),
            tools=self.tools,
            temperature=0.2,
            automatic_function_calling=self.types.AutomaticFunctionCallingConfig(disable=True),
        )
        last = None
        for attempt in (1, 2):                        # first try + exactly one retry
            try:
                t0 = time.time()
                resp = self.client.models.generate_content(model=self.model, contents=contents, config=config)
                if not resp.candidates or resp.candidates[0].content is None:
                    raise RuntimeError(f"empty response (prompt_feedback={resp.prompt_feedback})")
                log("model_response", attempt=attempt, ms=int((time.time() - t0) * 1000))
                calls = list(resp.function_calls or [])
                text = "" if calls else (resp.text or "")
                return resp.candidates[0].content, calls, text
            except Exception as e:                    # bad key, 429, timeout, network, empty
                last = e
                log("model_error", attempt=attempt, error_type=type(e).__name__, error=str(e)[:500])
                if attempt == 1:
                    time.sleep(1.0)
        raise LLMUnavailable(_classify(last), str(last)[:500])


def _classify(e):
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    msg = str(e).lower()
    if code in (401, 403) or "api key" in msg or "permission" in msg:
        return "auth"
    if code == 429 or "quota" in msg or "rate" in msg:
        return "rate_limit"
    if "timeout" in msg or "timed out" in msg or "deadline" in msg:
        return "timeout"
    return "upstream"


# ----------------------------------------------------------------- agent
class Session:
    def __init__(self):
        self.history = []           # SDK Content objects, committed only after a successful turn
        self.unit_rents = {}        # unit_id -> rent (None = not on file), from tool results
        self.user_numbers = set()
        self.shown_unit_ids = set() # units already listed to the user in an earlier reply
        self.lock = threading.Lock()


class LeasingAgent:
    MAX_STEPS = 8

    def __init__(self, llm):
        self.llm = llm
        self.sessions = {}
        self._sessions_lock = threading.Lock()

    def _session(self, sid):
        with self._sessions_lock:
            return self.sessions.setdefault(sid, Session())

    def chat(self, session_id, message):
        turn_id = uuid.uuid4().hex[:8]
        log = lambda event, **f: log_event(event, session_id, turn_id, **f)
        s = self._session(session_id)
        with s.lock:
            log("incoming_message", message=message)
            s.user_numbers |= guard.numbers_in(message)
            contents = list(s.history) + [self.llm.user_text(message)]
            turn_units = {}         # unit_id -> unit dict, from this turn's tool results

            text = self._run(contents, s, turn_units, log)

            problems = guard.check(text, s.unit_rents, s.user_numbers)
            if problems:
                log("guard_violation", reply=text, problems=problems)
                fix = ("[Automated check] Your reply stated prices that are not in the tool results: "
                       + " ".join(problems)
                       + " Rewrite it using only price_display values from the tools; "
                         "for units with no rent say 'price not on file, I'll check'.")
                retry_contents = contents + [self.llm.model_text(text), self.llm.user_text(fix)]
                text = self._run(retry_contents, s, turn_units, log)
                problems = guard.check(text, s.unit_rents, s.user_numbers)
                if problems:
                    log("guard_fallback", reply=text, problems=problems)
                    text = guard.fallback_reply(list(turn_units.values()))

            unpriced = [u for u in turn_units.values() if u.get("rent") is None
                        and u.get("_from") == "search_units"
                        and u["unit_id"] not in s.shown_unit_ids]
            missing = guard.missing_unpriced(text, unpriced)
            if missing:
                log("guard_unpriced_appended", unit_ids=[u["unit_id"] for u in missing])
                text = text.rstrip() + "\n" + guard.unpriced_footer(missing)

            detail_unpriced = [u for u in turn_units.values() if u.get("rent") is None
                               and u.get("_from") == "get_unit_details"]
            if detail_unpriced and "not on file" not in text.lower():
                log("guard_unpriced_appended", unit_ids=[u["unit_id"] for u in detail_unpriced])
                text = text.rstrip() + "\n\n" + "\n".join(
                    f"Price for Unit {u['unit_number']} (ID {u['unit_id']}): not on file, I'll check."
                    for u in detail_unpriced)

            # Commit: everything up to the model's last tool use, then the final checked text.
            s.history = contents + [self.llm.model_text(text)]
            s.shown_unit_ids |= guard.mentioned_ids(text)
            log("final_reply", reply=text)
            return text

    def _run(self, contents, s, turn_units, log):
        """Call the model, execute tool calls, repeat until it answers in text. Mutates contents."""
        for step in range(self.MAX_STEPS):
            content, calls, text = self.llm.generate(contents, log)
            if not calls:
                return text.strip() or "Sorry, I didn't catch that. Could you rephrase?"
            contents.append(content)
            results = []
            for call in calls:
                args = dict(call.args or {})
                log("tool_call", tool=call.name, args=args)
                result, ignored = tools.dispatch(call.name, args)
                if ignored:
                    log("tool_args_ignored", tool=call.name, ignored=ignored)
                log("tool_result", tool=call.name, result=result)
                self._remember_units(call.name, result, s, turn_units)
                results.append((call, result))
            contents.append(self.llm.tool_results(results))
        log("max_steps_reached", steps=self.MAX_STEPS)
        return "Sorry, that took more steps than expected. Could you ask in a simpler way?"

    @staticmethod
    def _remember_units(tool_name, result, s, turn_units):
        units = []
        if tool_name == "search_units":
            units = result.get("units_with_price", []) + result.get("units_price_not_on_file", [])
        elif tool_name == "get_unit_details" and "unit" in result:
            units = [result["unit"]]
        for u in units:
            s.unit_rents[u["unit_id"]] = u["rent"]
            turn_units[u["unit_id"]] = {**u, "_from": tool_name}
