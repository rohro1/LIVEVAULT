CREATE TABLE IF NOT EXISTS accounts (
  username TEXT PRIMARY KEY,
  active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS recordings (
  drive_id TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  room_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  part_number INTEGER NOT NULL,
  filename TEXT NOT NULL,
  drive_url TEXT NOT NULL,
  file_size INTEGER NOT NULL,
  duration_seconds REAL NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS recordings_created_at_idx
  ON recordings(created_at DESC);

CREATE TABLE IF NOT EXISTS login_attempts (
  ip_hash TEXT PRIMARY KEY,
  window_start INTEGER NOT NULL,
  failures INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dashboard_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_manual_dispatches (
  scope TEXT PRIMARY KEY,
  dispatched_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS account_status (
  username TEXT PRIMARY KEY,
  online INTEGER CHECK (online IN (0, 1) OR online IS NULL),
  recorder_status TEXT NOT NULL DEFAULT 'idle',
  recording_started_at TEXT,
  checked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS activity_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS activity_logs_created_at_idx
  ON activity_logs(created_at DESC);
