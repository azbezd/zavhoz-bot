PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'unknown',
  status TEXT NOT NULL DEFAULT 'stock',
  total_qty REAL NOT NULL DEFAULT 0,
  available_qty REAL NOT NULL DEFAULT 0,
  unit TEXT NOT NULL DEFAULT 'pcs',
  location TEXT NOT NULL DEFAULT 'unsorted',
  notes TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  price_rub REAL NOT NULL DEFAULT 0,
  last_verified_at TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inv_sessions (
  user_id INTEGER PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  started_at TEXT NOT NULL,
  last_action_at TEXT NOT NULL,
  seen INTEGER NOT NULL DEFAULT 0,
  await_qty_for TEXT NOT NULL DEFAULT '',
  last_prompt_message_id INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS item_tags (
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  tag TEXT NOT NULL,
  PRIMARY KEY (item_id, tag)
);

CREATE TABLE IF NOT EXISTS item_photos (
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  PRIMARY KEY (item_id, path)
);

CREATE TABLE IF NOT EXISTS item_manuals (
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  url_or_path TEXT NOT NULL,
  PRIMARY KEY (item_id, url_or_path)
);

CREATE TABLE IF NOT EXISTS item_sources (
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'reference',
  title TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (item_id, url)
);

CREATE TABLE IF NOT EXISTS item_knowledge (
  item_id TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  summary TEXT NOT NULL DEFAULT '',
  specs_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL,
  qty REAL NOT NULL DEFAULT 1,
  role TEXT NOT NULL DEFAULT '',
  since TEXT NOT NULL DEFAULT '',
  removable INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  notes TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_user_id INTEGER NOT NULL,
  telegram_chat_id INTEGER NOT NULL,
  message_text TEXT NOT NULL DEFAULT '',
  photo_paths_json TEXT NOT NULL DEFAULT '[]',
  proposal_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL,
  applied_at TEXT
);

CREATE TABLE IF NOT EXISTS chat_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_user_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
