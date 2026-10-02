-- Immediate fixes for N1 and N3. Applied by apply_fix.py to a COPY of the
-- snapshot (incident_fixed.db) so incident.db stays untouched for re-running
-- the evidence queries. In production this would run against the live DB.
-- Timestamps are the snapshot time so the audit trail lines up.

BEGIN;

-- N1: put the quiet window back to 21:00-09:00 (what deploy set on 2026-05-12),
-- and record the change in the audit table like every other settings change.
INSERT INTO settings_audit (key, old_value, new_value, changed_by, changed_at)
SELECT 'quiet_start', value, '21', 'incident-N1-revert', '2026-08-12T21:09:44'
FROM settings WHERE key = 'quiet_start';
INSERT INTO settings_audit (key, old_value, new_value, changed_by, changed_at)
SELECT 'quiet_end', value, '9', 'incident-N1-revert', '2026-08-12T21:09:44'
FROM settings WHERE key = 'quiet_end';
UPDATE settings SET value = '21' WHERE key = 'quiet_start';
UPDATE settings SET value = '9'  WHERE key = 'quiet_end';

-- N3: Dana's appointments carry a fabricated last_reminded (2027-03-14) with no
-- message behind it. Clear it so the column tells the truth. Only 259 is still
-- in the future, so only 259 will actually be picked up by notifyd.
UPDATE appointments
SET last_reminded = NULL
WHERE id IN (86, 202, 259)
  AND last_reminded = '2027-03-14T09:00:00'
  AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.appointment_id = appointments.id);

COMMIT;

-- N2 has no database fix: cron-v1 is a separate process that ignores
-- last_reminded, so the fix is to find and stop it (see INCIDENT.md).
