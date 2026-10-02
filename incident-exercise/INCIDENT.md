# notifyd incident report

## How to reproduce everything here

```
python evidence.py          # every query below, with its output (read-only on incident.db)
python evidence.py N2       # one ticket only
python apply_fix.py         # applies fix.sql to a copy (incident_fixed.db) and runs notifyd
```

No sqlite3 CLI is needed; the queries are plain SQL and can also be pasted into one.

**Reference time.** The snapshot was taken at `2026-08-12T21:09:44` server time
(America/Chicago). The last notifyd heartbeat is 20:54:44 on a 15 minute cadence,
and the audit timestamps sit exact whole days before 21:09:44. All queries use
this fixed time instead of `now()`, so they give the same output whenever they
are re-run. Running `python notifyd.py stats` or `run` against today's clock
shows nothing due, because every tour in the snapshot is in the past relative
to today.

## Summary

| Ticket | Root cause | Code at fault? |
|---|---|---|
| N1 | `quiet_start` and `quiet_end` were swapped through the ops console on 2026-08-08 14:32, turning "quiet 21:00 to 09:00" into "quiet 09:00 to 21:00" | No. Config change |
| N2 | A second sender, `cron-v1`, still reminds every `legacy_form` appointment. It ignores `last_reminded` and does not set it | Not notifyd. A legacy process |
| N3 | All three of Dana's appointments have `last_reminded = 2027-03-14T09:00:00`, a future date with no message behind it, so notifyd's `last_reminded IS NULL` filter skips her | No. Bad data |

The audit was right: notifyd behaves exactly as written in all three cases. Every
ticket comes from something around it: its configuration, another sender, and
its input data.

---

## N1. Reminders went nocturnal

### Root cause

On 2026-08-08 at 14:32, about four days before the snapshot, someone using the
ops console swapped the two quiet-hours settings. notifyd re-reads settings on
every tick, so the change took effect immediately with no deploy, which matches
"nobody deployed anything".

`in_quiet_hours` treats `start < end` as a same-day window. With `quiet_start=9`
and `quiet_end=21`, quiet hours are now 09:00 to 21:00 in the client's time, which
is the whole working day. Anything that comes due during the day is deferred
until 21:00 and sent then.

### Evidence

**N1.1 Settings change history.** The swap is rows 4 and 5:

```
id | key         | old_value | new_value | changed_by  | changed_at
1  | quiet_start | NULL      | 21        | deploy      | 2026-05-12T21:09:44
2  | quiet_end   | NULL      | 9         | deploy      | 2026-05-12T21:09:44
3  | batch_limit | 10        | 25        | deploy      | 2026-07-12T21:09:44
4  | quiet_start | 21        | 9         | ops-console | 2026-08-08T14:32:05
5  | quiet_end   | 9         | 21        | ops-console | 2026-08-08T14:32:16
```

**N1.2 Current settings:** `quiet_start = 9`, `quiet_end = 21`.

**N1.3 Hour notifyd sent reminders.** Before the change, sends are spread over all
24 hours. After it, every single send (14 of 14) happened in the 21:00 hour:

```
period | hour | sent
before | 00 .. 23 | 240 total, every hour of the day represented
after  | 21   | 14
```

(Full per-hour output in `python evidence.py N1`.)

**N1.4 Lead time collapsed.** Reminders used to arrive about 24h before the tour.
Now they arrive whenever 21:00 comes, as little as 3.9h ahead:

```
period | reminders | min_lead_h | avg_lead_h | max_lead_h
before | 240       | 23.3       | 23.7       | 24.0
after  | 14        | 3.9        | 15.3       | 24.0
```

**N1.6 The worker itself is healthy.** 192 heartbeats from 2026-08-10T21:09:44
to 2026-08-12T20:54:44 with no gap over 16 minutes, so this is not a stalled
or restarted worker.

### What the data does not show

The ticket says clients with morning tours get **no reminder at all**. The
snapshot does not support that part. N1.5 lists every tour between the change
and the snapshot: all 22 have a reminder. Morning tours were reminded the
previous night, for example:

```
id  | tour_at             | last_reminded       | lead_h
256 | 2026-08-10T09:01:39 | 2026-08-09T21:11:22 | 11.8
254 | 2026-08-11T09:32:36 | 2026-08-10T21:23:01 | 12.2
255 | 2026-08-12T09:22:35 | 2026-08-11T21:14:00 | 12.1
```

My reading is that clients experience a 9pm text the night before a 9am tour
as not having been reminded in the normal way, but I cannot show from the data
that any reminder was missed. One case where a reminder would genuinely be
lost under the swapped settings: a daytime tour booked less than about 12 hours
in advance, because it would spend its whole due window inside quiet hours.
No appointment in the snapshot was booked less than 24h ahead, so this has not
happened yet here, but it is a real risk while the setting stays swapped.

### Immediate fix

Set the values back (in `fix.sql`, with matching audit rows):

```sql
UPDATE settings SET value = '21' WHERE key = 'quiet_start';
UPDATE settings SET value = '9'  WHERE key = 'quiet_end';
```

No restart is needed; the next tick reads them. `apply_fix.py` shows the
restored behaviour: at the 21:09:44 tick, a Los Angeles client (local 19:09,
outside quiet hours) is sent immediately, and a Chicago client is held until
09:00.

### Prevention

- Validate settings on write and on read: reject a quiet window longer than,
  say, 12 hours, or rename the pair to `send_window_start/end` so the meaning
  is not inverted by a simple swap. Refuse to start a tick on an invalid value
  and alert instead.
- `changed_by = 'ops-console'` does not say who. Record the person, and require
  a second approver for settings that change who gets messaged and when.
- Alert on outcomes, not just liveness: median reminder lead time below 20h,
  or more than half of a day's sends in a single hour. Either alert would have
  fired on 2026-08-09.

---

## N2. Chronic double reminders

### Root cause

There are two senders writing to `messages`: `notifyd` and `cron-v1`. `cron-v1`
only ever reminds appointments with `source = 'legacy_form'` and it does so for
every one of them. It neither checks nor sets `last_reminded`, so it and
notifyd each send their own reminder. The "kind of client" nobody could pin
down is: booked through the legacy form.

### Evidence

**N2.1 Two senders, both active since the start of the data:**

```
sender  | messages | first               | last
cron-v1 | 44       | 2026-06-13T03:37:39 | 2026-08-12T12:57:47
notifyd | 254      | 2026-06-13T02:55:39 | 2026-08-12T21:08:23
```

**N2.2 Double reminders are exactly the legacy_form appointments:**

```
source      | appointments | double_reminded | got_cron_v1
legacy_form | 45           | 44              | 44
web         | 218          | 0               | 0
```

The 45th legacy_form appointment (id 245) has not been reminded by anyone yet;
its tour is 2026-08-14T22:14:40, outside the 24h window (N2.6). Every
legacy_form appointment that has come due got two reminders; no web appointment
ever did.

**N2.3 Every double is one notifyd plus one cron-v1:**

```
senders         | appointments
notifyd+cron-v1 | 43
cron-v1+notifyd | 1
```

**N2.4 "A few minutes apart":** before the N1 change, the gap was 6 to 50 minutes
(average 27) across 43 pairs.

**N2.5 cron-v1 ignores and does not write last_reminded:**

```
appointment_id | sender  | sent_at             | last_reminded
250            | notifyd | 2026-08-07T15:27:58 | 2026-08-07T15:27:58
250            | cron-v1 | 2026-08-07T16:06:58 | 2026-08-07T15:27:58
257            | cron-v1 | 2026-08-12T12:57:47 | 2026-08-12T21:08:23
257            | notifyd | 2026-08-12T21:08:23 | 2026-08-12T21:08:23
```

On 250, `last_reminded` was already set when cron-v1 sent anyway. On 257,
cron-v1 sent first but `last_reminded` holds notifyd's timestamp, so cron-v1
did not set it and notifyd still saw the appointment as unreminded. Note 257
also shows N1 and N2 interacting: the duplicate came 8 hours later instead of
minutes later.

cron-v1 writes no heartbeats (all 192 heartbeats are `notifyd@prod-sched-1`),
so the database cannot tell us which host runs it.

### Immediate fix

Find and stop cron-v1 before appointment 245 comes due on 2026-08-13 at 22:14.
Because it leaves no heartbeat, locate it on the scheduler hosts, for example
`crontab -l` for each service user, `/etc/cron.d/`, and a search for the
string `cron-v1` in deployed scripts. Disable it rather than delete it until
someone confirms notifyd covers every legacy_form case (N2.2 says it does:
notifyd reminded all 44).

There is no safe database-only fix: notifyd already behaves correctly, and
cron-v1 does not read anything we could flip.

### Prevention

- One writer per job: give each sender its own credential and only grant
  notifyd's credential INSERT on `messages`. An unknown sender would then fail
  loudly instead of quietly duplicating.
- Enforce at most one reminder per appointment in the schema:
  `CREATE UNIQUE INDEX one_reminder ON messages(appointment_id);` (after
  deduplicating history). The second sender's insert fails and shows up in
  its logs.
- Every sender writes heartbeats; alert on any `messages.sender` with no
  matching heartbeat worker.
- When a system is replaced, the migration checklist includes "old sender
  confirmed off" with evidence, such as zero rows from it for 7 days.

---

## N3. Dana never gets reminded

### Root cause

All three of Dana's appointments have `last_reminded = '2027-03-14T09:00:00'`.
That date is seven months after the snapshot and after every one of her tours.
No message was ever sent to her. notifyd only selects rows where
`last_reminded IS NULL`, so it skips her every time. Her phone number is
irrelevant; she never reaches the sending step.

### Evidence

**N3.1 Dana's appointments:**

```
id  | phone      | source | booked_at           | tour_at             | last_reminded
86  | 2145552427 | web    | 2026-06-28T21:09:44 | 2026-07-03T21:09:44 | 2027-03-14T09:00:00
202 | 2145552427 | web    | 2026-07-26T21:09:44 | 2026-07-31T21:09:44 | 2027-03-14T09:00:00
259 | 2145552427 | web    | 2026-08-08T19:09:44 | 2026-08-13T19:09:44 | 2027-03-14T09:00:00
```

**N3.2 Messages ever sent to her phone or appointments:** `0`.

**N3.3 Appointments marked reminded with no message, or marked after the tour:**
exactly Dana's three rows and nobody else. This is an isolated data problem,
not a pattern affecting other clients.

**N3.4 What notifyd sees for her next tour:** appointment 259 is inside the 24h
window but `skipped: last_reminded is not NULL`.

I can show that the value is wrong, but not who wrote it. There is no audit
table for `appointments`, and notifyd only ever writes `now()` into the column,
so it did not come from notifyd. The identical value on all three rows suggests
something applied at booking time or a manual edit to her customer record (for
example a do-not-contact or snooze date that was entered into the wrong field).
That is a hypothesis to check with whoever manages bookings, not a finding.

### Immediate fix (done, on a copy)

```sql
UPDATE appointments SET last_reminded = NULL
WHERE id IN (86, 202, 259)
  AND last_reminded = '2027-03-14T09:00:00'
  AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.appointment_id = appointments.id);
```

Clearing the two past rows makes the history truthful; notifyd will not touch
them because their tours are in the past. `python apply_fix.py` applies this and
the N1 fix to `incident_fixed.db`, then runs the **unmodified** `notifyd.cmd_run`
every 15 minutes from the snapshot time. Result:

```
[tick at 2026-08-12T21:09:44]  ... 1 deferred (quiet hours)     <- Dana, 21:09 is quiet
...
Dana reminded: (302, 259, '2145552427', 'notifyd', '2026-08-13T09:09:44')
body: "Hi Dana! Reminder: your apartment tour is at 2026-08-13 19:09. Reply R to reschedule."
```

She is reminded at 09:09 on the day of her 19:09 tour, the first tick after
quiet hours end. If the business wants her reminded tonight instead, apply the
N3 fix before the N1 fix; under the current swapped settings 21:09 is outside
quiet hours and she would be sent immediately.

### Prevention

- Only the sender should write `last_reminded`. Better, drop the column and
  derive "reminded" from `messages`, so "marked reminded" can never exist
  without a message.
- Add `CHECK (last_reminded IS NULL OR last_reminded <= tour_at)`; a future
  date like this one would have been rejected on write.
- Daily check: any tour in the next 12h with no message row. Run N3.3 as a
  standing alert.
- Add an audit trail on `appointments` so the next case like this can be traced
  to its writer.

---

## Things in the data I could not explain

These do not change the three diagnoses above, but the brief asks for evidence
over assumption, so I am flagging them rather than leaving them out.

1. **Stored message bodies do not match notifyd's template (X.1).** Every
   message in the snapshot, from both senders, reads
   `Reminder: your apartment tour is coming up.` The audited code writes
   `Hi <name>! Reminder: your apartment tour is at <time>. Reply R to
   reschedule.`, which is what appears when the code is actually run
   (message 302 above). Either the stored body is being rewritten after insert,
   or the code that produced these rows is not exactly the audited code. Worth
   confirming against the provider's delivery log.

2. **Historical sends do not fully respect quiet hours (X.2).** Using notifyd's
   own timezone offsets, 108 of 240 sends before the change and 8 of 14 after it
   fall inside the quiet window that was configured at the time. For example,
   after the change, Los Angeles clients were sent at 21:xx server time, which
   is 19:xx for them and should have been deferred. So either the production
   host's clock is not America/Chicago as the code assumes, or the `tz` values
   were different when those messages went out. N1's diagnosis rests on the
   before/after shape (N1.3, N1.4), which holds regardless, but the exact
   send times should not be taken as proof of how the quiet-hours function
   behaved in production.
