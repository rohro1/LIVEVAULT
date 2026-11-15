ALTER TABLE account_status ADD COLUMN last_online_at TEXT;

UPDATE account_status
SET last_online_at = checked_at
WHERE online = 1;
