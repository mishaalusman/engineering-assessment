#!/usr/bin/env python3
"""Reconstruct a conversation from the log.

    python replay.py <session_id>
"""
import json
import sys

from agent import LOG_FILE

sid = sys.argv[1]
with open(LOG_FILE, encoding="utf-8") as f:
    for line in f:
        rec = json.loads(line)
        if rec.get("session_id") != sid:
            continue
        ev, ts = rec["event"], rec["ts"]
        if ev == "incoming_message":
            print(f"\n{ts}  USER: {rec['message']}")
        elif ev == "tool_call":
            print(f"{ts}    -> {rec['tool']}({json.dumps(rec['args'])})")
        elif ev == "tool_result":
            print(f"{ts}    <- {json.dumps(rec['result'])[:300]}")
        elif ev == "final_reply":
            print(f"{ts}  AGENT: {rec['reply']}")
        else:
            print(f"{ts}    [{ev}] " + json.dumps({k: v for k, v in rec.items()
                                                  if k not in ('ts', 'event', 'session_id', 'turn_id')})[:300])
