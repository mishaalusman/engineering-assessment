#!/usr/bin/env python3
"""Runs every query cited in INCIDENT.md against incident.db and prints the output.

Usage:
    python evidence.py              # all queries
    python evidence.py N1 N3        # only some tickets
    python evidence.py --db other.db

Read-only: opens the database with mode=ro so nothing can change it.
"""
import argparse
import sqlite3

# The snapshot was taken at 2026-08-12T21:09:44 (last notifyd heartbeat tick is
# 20:54:44 on a 15 minute cadence, and the settings_audit timestamps are exact
# multiples of days before 21:09:44). Queries use this fixed time instead of
# now() so they give the same answer whenever they are re-run.
SNAPSHOT = "2026-08-12T21:09:44"

QUERIES = [
    # ---------------------------------------------------------------- N1
    ("N1", "N1.1 Settings change history", """
SELECT id, key, old_value, new_value, changed_by, changed_at
FROM settings_audit ORDER BY id;"""),

    ("N1", "N1.2 Current settings", """
SELECT key, value FROM settings ORDER BY key;"""),

    ("N1", "N1.3 Hour of day notifyd sent reminders, before vs after the 2026-08-08 change", """
SELECT CASE WHEN sent_at >= '2026-08-08T14:32:16' THEN 'after' ELSE 'before' END AS period,
       substr(sent_at, 12, 2) AS hour, COUNT(*) AS sent
FROM messages WHERE sender = 'notifyd'
GROUP BY 1, 2 ORDER BY 1 DESC, 2;"""),

    ("N1", "N1.4 Hours between reminder and tour, before vs after", """
SELECT CASE WHEN m.sent_at >= '2026-08-08T14:32:16' THEN 'after' ELSE 'before' END AS period,
       COUNT(*) AS reminders,
       ROUND(MIN((julianday(a.tour_at) - julianday(m.sent_at)) * 24), 1) AS min_lead_h,
       ROUND(AVG((julianday(a.tour_at) - julianday(m.sent_at)) * 24), 1) AS avg_lead_h,
       ROUND(MAX((julianday(a.tour_at) - julianday(m.sent_at)) * 24), 1) AS max_lead_h
FROM messages m JOIN appointments a ON a.id = m.appointment_id
WHERE m.sender = 'notifyd' GROUP BY 1 ORDER BY 1 DESC;"""),

    ("N1", "N1.5 Every tour since the change: when (if ever) was it reminded?", """
SELECT a.id, a.tz, a.tour_at, a.last_reminded,
       ROUND((julianday(a.tour_at) - julianday(a.last_reminded)) * 24, 1) AS lead_h
FROM appointments a
WHERE a.tour_at >= '2026-08-08T14:32:16' AND a.tour_at <= '2026-08-12T21:09:44'
ORDER BY a.tour_at;"""),

    ("N1", "N1.6 Is the worker running? Heartbeat gaps over 16 minutes", """
SELECT COUNT(*) AS heartbeats, MIN(ts) AS first, MAX(ts) AS last,
       (SELECT COUNT(*) FROM (SELECT ts, LAG(ts) OVER (ORDER BY ts) AS prev FROM heartbeats)
        WHERE (julianday(ts) - julianday(prev)) * 1440 > 16) AS gaps_over_16_min
FROM heartbeats;"""),

    # ---------------------------------------------------------------- N2
    ("N2", "N2.1 Who sends reminders?", """
SELECT sender, COUNT(*) AS messages, MIN(sent_at) AS first, MAX(sent_at) AS last
FROM messages GROUP BY sender;"""),

    ("N2", "N2.2 Appointments with more than one reminder, by booking source", """
SELECT a.source,
       COUNT(*) AS appointments,
       SUM((SELECT COUNT(*) FROM messages m WHERE m.appointment_id = a.id) > 1) AS double_reminded,
       SUM(EXISTS (SELECT 1 FROM messages m
                   WHERE m.appointment_id = a.id AND m.sender = 'cron-v1')) AS got_cron_v1
FROM appointments a GROUP BY a.source;"""),

    ("N2", "N2.3 Sender pairs on double-reminded appointments", """
SELECT senders, COUNT(*) AS appointments FROM (
    SELECT appointment_id, group_concat(sender, '+') AS senders
    FROM (SELECT appointment_id, sender FROM messages ORDER BY appointment_id, sent_at)
    GROUP BY appointment_id HAVING COUNT(*) > 1)
GROUP BY senders;"""),

    ("N2", "N2.4 Minutes between the two reminders (before the N1 change)", """
SELECT COUNT(*) AS pairs,
       ROUND(MIN(gap), 1) AS min_min, ROUND(AVG(gap), 1) AS avg_min, ROUND(MAX(gap), 1) AS max_min
FROM (SELECT (julianday(MAX(sent_at)) - julianday(MIN(sent_at))) * 1440 AS gap
      FROM messages GROUP BY appointment_id
      HAVING COUNT(*) > 1 AND MAX(sent_at) < '2026-08-08T14:32:16');"""),

    ("N2", "N2.5 cron-v1 ignores last_reminded and does not set it (appointments 250, 257)", """
SELECT m.appointment_id, a.source, m.sender, m.sent_at, a.last_reminded
FROM messages m JOIN appointments a ON a.id = m.appointment_id
WHERE m.appointment_id IN (250, 257) ORDER BY m.appointment_id, m.sent_at;"""),

    ("N2", "N2.6 Upcoming legacy_form tours still exposed to a double reminder", """
SELECT id, source, tour_at, last_reminded FROM appointments
WHERE source = 'legacy_form' AND tour_at > '2026-08-12T21:09:44';"""),

    # ---------------------------------------------------------------- N3
    ("N3", "N3.1 Dana's appointments", """
SELECT id, client_name, phone, tz, source, booked_at, tour_at, last_reminded
FROM appointments WHERE client_name LIKE '%Whitfield%' ORDER BY tour_at;"""),

    ("N3", "N3.2 Messages ever sent to Dana's phone or appointments", """
SELECT COUNT(*) AS messages FROM messages
WHERE phone = '2145552427'
   OR appointment_id IN (SELECT id FROM appointments WHERE client_name LIKE '%Whitfield%');"""),

    ("N3", "N3.3 Appointments marked reminded with no message behind them, or marked after the tour", """
SELECT a.id, a.client_name, a.booked_at, a.tour_at, a.last_reminded
FROM appointments a
WHERE a.last_reminded IS NOT NULL
  AND (NOT EXISTS (SELECT 1 FROM messages m WHERE m.appointment_id = a.id)
       OR a.last_reminded > a.tour_at);"""),

    ("N3", "N3.4 Would notifyd select Dana's next tour right now? (same WHERE clause as due_appointments)", """
SELECT id, tour_at, last_reminded,
       CASE WHEN last_reminded IS NULL THEN 'selected' ELSE 'skipped: last_reminded is not NULL' END AS notifyd_sees
FROM appointments
WHERE client_name LIKE '%Whitfield%'
  AND tour_at > '2026-08-12T21:09:44' AND tour_at <= '2026-08-13T21:09:44';"""),

    # ---------------------------------------------------------------- data caveats
    ("X", "X.1 Message bodies stored per sender", """
SELECT sender, body, COUNT(*) AS n FROM messages GROUP BY sender, body;"""),

    ("X", "X.2 notifyd sends that fall inside the quiet window in force at the time (client local hour, using notifyd's TZ_OFFSET_HOURS)", """
WITH s AS (
  SELECT m.sent_at >= '2026-08-08T14:32:16' AS after_change,
         (CAST(substr(m.sent_at, 12, 2) AS INTEGER)
          + CASE a.tz WHEN 'America/New_York' THEN 1 WHEN 'America/Denver' THEN -1
                      WHEN 'America/Los_Angeles' THEN -2 ELSE 0 END + 24) % 24 AS local_hour
  FROM messages m JOIN appointments a ON a.id = m.appointment_id
  WHERE m.sender = 'notifyd')
SELECT CASE after_change WHEN 1 THEN 'after (quiet 09-21)' ELSE 'before (quiet 21-09)' END AS period,
       COUNT(*) AS sends,
       SUM(CASE WHEN after_change = 1 THEN local_hour >= 9 AND local_hour < 21
                ELSE local_hour >= 21 OR local_hour < 9 END) AS sent_inside_quiet_window
FROM s GROUP BY after_change ORDER BY after_change;"""),
]


def show(conn, title, sql):
    print(f"\n## {title}\n")
    print(sql.strip())
    print()
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    rows = [["NULL" if v is None else str(v) for v in r] for r in cur.fetchall()]
    widths = [max(len(c), *(len(r[i]) for r in rows)) if rows else len(c) for i, c in enumerate(cols)]
    print(" | ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("-+-".join("-" * w for w in widths))
    for r in rows:
        print(" | ".join(v.ljust(w) for v, w in zip(r, widths)))
    print(f"({len(rows)} rows)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("tickets", nargs="*", help="N1 N2 N3 X (default: all)")
    p.add_argument("--db", default="incident.db")
    args = p.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    for ticket, title, sql in QUERIES:
        if not args.tickets or ticket in args.tickets:
            show(conn, title, sql)


if __name__ == "__main__":
    main()
