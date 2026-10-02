# Leasing Agent API

A small HTTP service where Gemini chats with renters, searches the listings
database with tool calls, and books tours.

## Run it

Python 3.10 or newer.

```
pip install -r requirements.txt
python make_listings.py                     # creates listings.db
export GEMINI_API_KEY=your-key              # Windows PowerShell: $env:GEMINI_API_KEY="your-key"
uvicorn app:app --port 8000
```

Optional: `GEMINI_MODEL` (default `gemini-3.8-flash`). Older 2.5 models are being retired and may return a 404.
`TRANSCRIPTS.md` was produced with `GEMINI_MODEL=gemini-3.1-flash-lite`.

Talk to it:

```
curl -s localhost:8000/chat -H "Content-Type: application/json" \
  -d '{"session_id": "demo", "message": "Any 2-bed in Dallas under $2,000?"}'
```

Generate `TRANSCRIPTS.md` (server must be running):

```
python run_transcripts.py
```

Reconstruct any conversation from the log:

```
python replay.py demo
```

Run the tests (no API key needed; a scripted fake model stands in for Gemini):

```
pytest -q
```

## Files

| File | What it does |
|---|---|
| `app.py` | FastAPI app, `POST /chat`, all error responses |
| `agent.py` | Session memory, the tool-calling loop, Gemini wrapper with retry, logging |
| `tools.py` | `search_units`, `get_unit_details`, `request_tour` and their hard rules |
| `guard.py` | Checks every price in a reply against the database before it is sent |
| `run_transcripts.py` | Runs the three required conversations and writes `TRANSCRIPTS.md` |
| `replay.py` | Prints one session's messages, tool calls and replies from the log |
| `test_agent.py` | Offline tests for all four rules |

## Where each rule is enforced

**1. Grounding.** The prompt asks the model to copy `price_display`, but the
prompt is not the enforcement. After the model answers, `guard.check` pulls
every dollar amount out of the reply and rejects any that is not a rent the
tools returned in this session (numbers the user typed, like a $2,000 budget,
may be echoed). It also checks attribution line by line: a line about
`Unit X (ID n)` may only state unit n's rent, and a unit with NULL rent may
not have any amount next to it. On a violation the model gets one corrected
retry; if it fails again the reply is replaced by one built directly from the
tool data. Hiding is handled the same way: `search_units` never filters out
NULL-rent units (we cannot know they are over budget) and returns them in
their own list, and if the reply leaves any of them out, the code appends them
as "price not on file".

**2. Hard rules in code.** `request_tour` refuses when the unit does not exist,
is inactive, or the time is outside 09:00 to 18:00 (both ends allowed), and
also rejects past times and unreadable times. The checks are plain `if`
statements before the INSERT; there is no override parameter, and `dispatch`
drops any extra arguments the model invents (logged as `tool_args_ignored`).
The prompt tells the model to always attempt the booking through the tool, so
the refusal comes from code and appears in the log. A tour may only be
reported as booked if the tool returned `booked: true`.

**3. Resilience.** Each Gemini call has a 120 second timeout and is retried
exactly once after a 1 second pause. If both fail, `/chat` returns HTTP 503
with a friendly JSON message (different wording for bad key, rate limit,
timeout). Any other exception is caught, logged with its stack trace, and
returned as a JSON 500 with a reference id. Malformed requests get a JSON 400.
A failed turn is not saved to the session, so the conversation is not left
half-written.

**4. Observability.** Every step is one JSON line in `logs/agent.jsonl` and on
the console: `incoming_message`, `model_response`, `model_error`, `tool_call`
(with arguments), `tool_result`, any `guard_*` action, `final_reply`, and
errors. Each line carries `session_id` and a `turn_id`.

## Assumptions

- Tour times are building-local wall clock time. A timezone suffix is ignored.
- `min_beds` means at least that many bedrooms, so a 2-bed search also shows
  3-beds; the agent says which is which.
- Sessions live in memory and are lost on restart, as the brief allows.
- The guard recognises prices written with `$` or followed by "dollars",
  "/mo", "per month" or "a month". A bare number like "1850" with no unit is
  not treated as a price.
