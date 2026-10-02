#!/usr/bin/env python3
"""Apply fix.sql to a copy of the snapshot and prove Dana gets her reminder.

    python apply_fix.py

1. Copies incident.db -> incident_fixed.db (the original is never modified).
2. Runs fix.sql on the copy.
3. Runs the real, unmodified notifyd.cmd_run every 15 minutes (the heartbeat
   cadence) starting at the snapshot time, with the clock pinned to that
   simulated time. The snapshot is from 2026-08-12, so running notifyd against
   today's wall clock would find nothing due; pinning the clock is what makes
   the result reproducible.
4. Stops when Dana's appointment 259 has a reminder in messages, and prints it.
"""
import shutil
import sqlite3
from datetime import datetime as _real_datetime, timedelta

import notifyd

SRC, DST = "incident.db", "incident_fixed.db"
SNAPSHOT = _real_datetime.fromisoformat("2026-08-12T21:09:44")
DANA_NEXT = 259


class FrozenClock(_real_datetime):
    current = SNAPSHOT

    @classmethod
    def now(cls, tz=None):
        return cls.current


def main():
    shutil.copyfile(SRC, DST)
    conn = sqlite3.connect(DST)
    with open("fix.sql") as f:
        conn.executescript(f.read())
    print("settings after fix:", dict(conn.execute("SELECT key, value FROM settings")))
    print("appt 259 last_reminded after fix:",
          conn.execute("SELECT last_reminded FROM appointments WHERE id = ?", (DANA_NEXT,)).fetchone()[0])
    conn.close()

    notifyd.DB_PATH = DST
    notifyd.datetime = FrozenClock          # notifyd's own code is not edited

    t = SNAPSHOT
    while t < SNAPSHOT + timedelta(hours=24):
        FrozenClock.current = t
        print(f"\n[tick at {t.isoformat()}]")
        notifyd.cmd_run(None)
        conn = sqlite3.connect(DST)
        row = conn.execute(
            "SELECT id, appointment_id, phone, sender, sent_at FROM messages "
            "WHERE appointment_id = ?", (DANA_NEXT,)).fetchone()
        conn.close()
        if row:
            print(f"\nDana reminded: {row}")
            break
        t += timedelta(minutes=15)
    else:
        print("\nDana was NOT reminded within 24h of simulated ticks")


if __name__ == "__main__":
    main()
