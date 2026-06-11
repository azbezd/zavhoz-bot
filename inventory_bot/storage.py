import json
import os
import sqlite3
import re
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    db_path = os.environ.get("INVENTORY_DB", "inventory_bot/inventory.db")
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    with open(os.path.join(os.path.dirname(__file__), "schema.sql"), "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    """Add columns missing from tables created by older schema versions."""
    wanted = {
        "proposals": [
            ("draft_message_id", "INTEGER NOT NULL DEFAULT 0"),
        ],
        "projects": [
            ("desc_msg_id", "INTEGER NOT NULL DEFAULT 0"),
        ],
        "inv_sessions": [
            ("pass_no", "INTEGER NOT NULL DEFAULT 1"),
            ("skipped_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("current_item_id", "TEXT NOT NULL DEFAULT ''"),
            ("await_kind", "TEXT NOT NULL DEFAULT 'qty'"),
            ("pending_json", "TEXT NOT NULL DEFAULT ''"),
            ("mode", "TEXT NOT NULL DEFAULT 'walk'"),
            ("scope", "TEXT NOT NULL DEFAULT 'all'"),
        ],
    }
    for table, columns in wanted.items():
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def _yaml_scalar(raw: str):
    raw = raw.strip()
    if raw in ("null", "None"):
        return None
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw[1:-1]
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1]
    if re.fullmatch(r"-?\d+(\.\d+)?", raw):
        return float(raw) if "." in raw else int(raw)
    return raw


def seed_projects(conn) -> None:
    # Только первичное наполнение: удалённые пользователем проекты не воскрешаем.
    if conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"]:
        return
    now = utc_now()
    defaults = [
        ("project-freenet", "FreeNet", "LTE-роутер на Pi Zero 2W + SIM7600"),
        ("project-freenetbox", "FreeNetBox", "Роутер на Pi 5, алюминиевый корпус"),
        ("project-netbox", "NetBox", "Роутер на Pi 3B+"),
        ("project-dachanetbox", "DachaNetBox", "Роутер на даче, Pi 3B+"),
        ("project-ideas-lab", "Ideas Lab", "Эксперименты без отдельного проекта"),
    ]
    for project_id, name, description in defaults:
        conn.execute(
            """
            INSERT OR IGNORE INTO projects (id, name, description, status, notes, updated_at)
            VALUES (?, ?, ?, 'active', '', ?)
            """,
            (project_id, name, description, now),
        )
    conn.commit()


def seed_items_from_yaml(conn, repo_dir: str) -> int:
    existing = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    if existing:
        return 0
    path = os.path.join(repo_dir, "items.yaml")
    if not os.path.exists(path):
        return 0
    items = []
    current = None
    in_items = False
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip() == "items:":
                in_items = True
                continue
            if not in_items:
                continue
            if line.startswith("  - id:"):
                if current:
                    items.append(current)
                current = {"id": _yaml_scalar(line.split(":", 1)[1])}
                continue
            if current is None:
                continue
            if line.startswith("    ") and not line.startswith("      ") and ":" in line:
                key, value = line.strip().split(":", 1)
                if key in {"name", "category", "status", "total_qty", "available_qty", "unit", "location", "notes"}:
                    current[key] = _yaml_scalar(value)
    if current:
        items.append(current)
    now = utc_now()
    count = 0
    for item in items:
        if not item.get("id") or not item.get("name"):
            continue
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO items (
              id, name, category, status, total_qty, available_qty, unit,
              location, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["id"],
                item.get("name", ""),
                item.get("category", "unknown"),
                item.get("status", "stock"),
                float(item.get("total_qty") or 0),
                float(item.get("available_qty") or 0),
                item.get("unit", "pcs"),
                item.get("location", "unsorted"),
                item.get("notes", ""),
                now,
                now,
            ),
        )
        count += cur.rowcount
    conn.commit()
    return count


def save_proposal(conn, user_id: int, chat_id: int, text: str, photo_paths: list[str], proposal: dict) -> int:
    # Один активный черновик на пользователя: новый вытесняет старые,
    # чтобы случайное «да» через день не применило забытое.
    conn.execute(
        "UPDATE proposals SET status = 'discarded' WHERE telegram_user_id = ? AND status = 'pending'",
        (user_id,),
    )
    cur = conn.execute(
        """
        INSERT INTO proposals (
          telegram_user_id, telegram_chat_id, message_text, photo_paths_json,
          proposal_json, status, created_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (user_id, chat_id, text or "", json.dumps(photo_paths), json.dumps(proposal, ensure_ascii=False), utc_now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_pending(conn):
    return conn.execute(
        "SELECT id, created_at, proposal_json FROM proposals WHERE status = 'pending' ORDER BY id DESC LIMIT 20"
    ).fetchall()


def list_items(conn, limit: int = 80):
    return conn.execute(
        """
        SELECT id, name, category, status, available_qty, total_qty, unit, location
        FROM items
        WHERE status != 'retired'
        ORDER BY category, name
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def get_item(conn, item_id: str):
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return row


def item_first_photo(conn, item_id: str):
    row = conn.execute(
        "SELECT path FROM item_photos WHERE item_id = ? ORDER BY rowid LIMIT 1",
        (item_id,),
    ).fetchone()
    return row["path"] if row else None


def item_first_source(conn, item_id: str):
    row = conn.execute(
        "SELECT title, url FROM item_sources WHERE item_id = ? "
        "ORDER BY CASE kind WHEN 'purchase' THEN 0 ELSE 1 END, rowid LIMIT 1",
        (item_id,),
    ).fetchone()
    return (row["title"], row["url"]) if row else (None, None)


def inv_start(conn, user_id: int, chat_id: int, mode: str = "walk", scope: str = "all") -> None:
    now = utc_now()
    conn.execute(
        "INSERT INTO inv_sessions (user_id, chat_id, started_at, last_action_at, seen, await_qty_for, last_prompt_message_id, pass_no, skipped_json, current_item_id, mode, scope) "
        "VALUES (?, ?, ?, ?, 0, '', 0, 1, '[]', '', ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET chat_id = excluded.chat_id, started_at = excluded.started_at, "
        "last_action_at = excluded.last_action_at, seen = 0, await_qty_for = '', last_prompt_message_id = 0, "
        "pass_no = 1, skipped_json = '[]', current_item_id = '', mode = excluded.mode, scope = excluded.scope",
        (user_id, chat_id, now, now, mode, scope),
    )
    conn.execute("DELETE FROM inv_events WHERE user_id = ?", (user_id,))
    conn.commit()


def inv_new_count(conn) -> int:
    """Positions never verified at all (fresh arrivals)."""
    return conn.execute(
        "SELECT COUNT(*) AS c FROM items WHERE (last_verified_at IS NULL OR last_verified_at = '') "
        "AND status NOT IN ('retired', 'wishlist', 'in_use', 'lost')"
    ).fetchone()["c"]


def items_in_category(conn, category: str, offset: int = 0, limit: int = 8):
    return conn.execute(
        "SELECT id, name, available_qty, total_qty, unit FROM items "
        "WHERE category = ? AND status != 'retired' ORDER BY price_rub DESC, name "
        "LIMIT ? OFFSET ?",
        (category, limit + 1, offset),
    ).fetchall()


def categories_with_counts(conn):
    return conn.execute(
        "SELECT category, COUNT(*) AS cnt FROM items WHERE status != 'retired' "
        "GROUP BY category ORDER BY category"
    ).fetchall()


def inv_get(conn, user_id: int):
    return conn.execute("SELECT * FROM inv_sessions WHERE user_id = ?", (user_id,)).fetchone()


def inv_set_await(conn, user_id: int, item_id: str, kind: str = "qty") -> None:
    conn.execute(
        "UPDATE inv_sessions SET await_qty_for = ?, await_kind = ?, last_action_at = ? WHERE user_id = ?",
        (item_id, kind, utc_now(), user_id),
    )
    conn.commit()


def inv_clear_await(conn, user_id: int) -> None:
    conn.execute(
        "UPDATE inv_sessions SET await_qty_for = '', await_kind = 'qty', last_action_at = ? WHERE user_id = ?",
        (utc_now(), user_id),
    )
    conn.commit()


def inv_set_pending(conn, user_id: int, payload: dict) -> None:
    conn.execute(
        "UPDATE inv_sessions SET pending_json = ?, last_action_at = ? WHERE user_id = ?",
        (json.dumps(payload, ensure_ascii=False), utc_now(), user_id),
    )
    conn.commit()


def inv_get_pending(conn, user_id: int):
    sess = inv_get(conn, user_id)
    if not sess or not sess["pending_json"]:
        return None
    try:
        return json.loads(sess["pending_json"])
    except json.JSONDecodeError:
        return None


def inv_clear_pending(conn, user_id: int) -> None:
    conn.execute(
        "UPDATE inv_sessions SET pending_json = '', last_action_at = ? WHERE user_id = ?",
        (utc_now(), user_id),
    )
    conn.commit()


def item_projects(conn, item_id: str) -> list:
    rows = conn.execute(
        "SELECT DISTINCT p.name FROM item_usage u JOIN projects p ON p.id = u.project_id "
        "WHERE u.item_id = ? ORDER BY p.name",
        (item_id,),
    ).fetchall()
    return [r["name"] for r in rows]


def item_retire(conn, item_id: str) -> None:
    """Consumed/written off: gone for good, cannot be pulled back out of anything."""
    now = utc_now()
    conn.execute(
        "UPDATE items SET status = 'retired', total_qty = 0, available_qty = 0, "
        "last_verified_at = ?, updated_at = ? WHERE id = ?",
        (now, now, item_id),
    )
    conn.commit()


def item_split_to_project(conn, item_id: str, qty_used: float, project_id: str) -> tuple:
    """Move part of a position into a project as a NEW item (it stays in /list
    under the project and can be pulled back later); the rest stays free.
    Returns (new_id, remaining_qty)."""
    item = get_item(conn, item_id)
    if not item:
        raise ValueError(f"no item {item_id}")
    now = utc_now()
    new_id = next_item_id(conn)
    remaining = max(0.0, (item["total_qty"] or 0) - qty_used)
    conn.execute(
        "INSERT INTO items (id, name, category, status, total_qty, available_qty, unit, location, "
        "notes, description, price_rub, last_verified_at, created_at, updated_at) "
        "VALUES (?, ?, ?, 'in_use', ?, 0, ?, ?, '', ?, ?, ?, ?, ?)",
        (new_id, item["name"], item["category"], qty_used, item["unit"], item["location"],
         item["description"], item["price_rub"], now, now, now),
    )
    conn.execute(
        "INSERT INTO item_usage (item_id, project_id, qty, role, since, removable) VALUES (?, ?, ?, '', ?, 1)",
        (new_id, project_id, qty_used, now[:10]),
    )
    conn.execute(
        "INSERT OR IGNORE INTO item_photos (item_id, path) SELECT ?, path FROM item_photos WHERE item_id = ?",
        (new_id, item_id),
    )
    conn.execute(
        "INSERT OR IGNORE INTO item_sources (item_id, kind, title, url, notes) "
        "SELECT ?, kind, title, url, notes FROM item_sources WHERE item_id = ?",
        (new_id, item_id),
    )
    conn.execute(
        "UPDATE items SET total_qty = ?, available_qty = ?, last_verified_at = ?, updated_at = ? WHERE id = ?",
        (remaining, remaining, now, now, item_id),
    )
    conn.commit()
    return new_id, remaining


def item_set_category(conn, item_id: str, category: str) -> None:
    conn.execute(
        "UPDATE items SET category = ?, updated_at = ? WHERE id = ?",
        (category.strip().lower(), utc_now(), item_id),
    )
    conn.commit()


def inv_increment_seen(conn, user_id: int) -> None:
    conn.execute(
        "UPDATE inv_sessions SET seen = seen + 1, last_action_at = ? WHERE user_id = ?",
        (utc_now(), user_id),
    )
    conn.commit()


def inv_set_prompt_message(conn, user_id: int, message_id: int) -> None:
    conn.execute(
        "UPDATE inv_sessions SET last_prompt_message_id = ? WHERE user_id = ?",
        (message_id, user_id),
    )
    conn.commit()


def inv_finish(conn, user_id: int) -> None:
    conn.execute("DELETE FROM inv_sessions WHERE user_id = ?", (user_id,))
    conn.commit()


def inv_skipped(conn, user_id: int) -> list:
    sess = inv_get(conn, user_id)
    if not sess:
        return []
    try:
        return json.loads(sess["skipped_json"] or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


def inv_set_skipped(conn, user_id: int, item_ids: list) -> None:
    conn.execute(
        "UPDATE inv_sessions SET skipped_json = ?, last_action_at = ? WHERE user_id = ?",
        (json.dumps(item_ids), utc_now(), user_id),
    )
    conn.commit()


def inv_set_pass(conn, user_id: int, pass_no: int) -> None:
    conn.execute(
        "UPDATE inv_sessions SET pass_no = ?, last_action_at = ? WHERE user_id = ?",
        (pass_no, utc_now(), user_id),
    )
    conn.commit()


def inv_set_current(conn, user_id: int, item_id: str) -> None:
    conn.execute(
        "UPDATE inv_sessions SET current_item_id = ?, last_action_at = ? WHERE user_id = ?",
        (item_id, utc_now(), user_id),
    )
    conn.commit()


def inv_next_item(conn, user_id: int):
    """Pick the next item to verify this session, highest price first.

    Pass 1 walks everything except skipped items; pass 2 walks only skipped.
    """
    sess = inv_get(conn, user_id)
    if not sess:
        return None
    started = sess["started_at"]
    skipped = inv_skipped(conn, user_id)
    placeholders = ",".join("?" for _ in skipped) or "''"
    if sess["pass_no"] >= 2:
        if not skipped:
            return None
        clause = f"AND id IN ({placeholders})"
    else:
        clause = f"AND id NOT IN ({placeholders})" if skipped else ""
    scope_clause = "AND (last_verified_at IS NULL OR last_verified_at = '')" if sess["scope"] == "new" else ""
    return conn.execute(
        f"""
        SELECT id, name, category, status, total_qty, available_qty, unit, location, price_rub, description, last_verified_at, notes
        FROM items
        WHERE status NOT IN ('retired', 'wishlist', 'in_use', 'lost')
          AND (last_verified_at IS NULL OR last_verified_at = '' OR last_verified_at < ?)
          {scope_clause}
          {clause}
        ORDER BY price_rub DESC, name ASC
        LIMIT 1
        """,
        (started, *skipped),
    ).fetchone()


def inv_progress(conn, user_id: int) -> tuple:
    """(verified this session, total eligible)."""
    sess = inv_get(conn, user_id)
    if not sess:
        return (0, 0)
    started = sess["started_at"]
    scope_clause = ""
    if sess["scope"] == "new":
        # Новые = никогда не проверенные + те, что проверили в этой сессии.
        scope_clause = "AND (last_verified_at IS NULL OR last_verified_at = '' OR last_verified_at >= ?)"
    row = conn.execute(
        f"""
        SELECT
          SUM(CASE WHEN last_verified_at >= ? THEN 1 ELSE 0 END) AS done,
          COUNT(*) AS total
        FROM items WHERE status NOT IN ('retired', 'wishlist', 'in_use', 'lost')
        {scope_clause}
        """,
        (started, started) if scope_clause else (started,),
    ).fetchone()
    return (row["done"] or 0, row["total"] or 0)


def inv_log_event(conn, user_id: int, item_id: str, item_name: str, action: str,
                  old_total=None, new_total=None) -> None:
    conn.execute(
        "INSERT INTO inv_events (user_id, item_id, item_name, action, old_total, new_total, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, item_id, item_name, action, old_total, new_total, utc_now()),
    )
    conn.commit()


def inv_events_for(conn, user_id: int):
    return conn.execute(
        "SELECT * FROM inv_events WHERE user_id = ? ORDER BY id", (user_id,)
    ).fetchall()


def inv_mark_present(conn, item_id: str) -> None:
    conn.execute(
        "UPDATE items SET last_verified_at = ?, updated_at = ? WHERE id = ?",
        (utc_now(), utc_now(), item_id),
    )
    conn.commit()


def inv_mark_qty(conn, item_id: str, new_total: float) -> None:
    """Set total to new_total keeping the reserved (in-project) part intact.
    A positive count on a lost item means it was found again."""
    row = conn.execute("SELECT total_qty, available_qty, status FROM items WHERE id = ?", (item_id,)).fetchone()
    reserved = max(0.0, (row["total_qty"] or 0) - (row["available_qty"] or 0)) if row else 0.0
    new_available = max(0.0, new_total - reserved)
    status_fix = ", status = 'stock'" if (row and row["status"] == "lost" and new_total > 0) else ""
    conn.execute(
        f"UPDATE items SET total_qty = ?, available_qty = ?{status_fix}, "
        "last_verified_at = ?, updated_at = ? WHERE id = ?",
        (new_total, new_available, utc_now(), utc_now(), item_id),
    )
    conn.commit()


def item_append_note(conn, item_id: str, text: str) -> None:
    row = conn.execute("SELECT notes FROM items WHERE id = ?", (item_id,)).fetchone()
    old = (row["notes"] or "").strip() if row else ""
    stamp = utc_now()[:10]
    new = (old + "\n" if old else "") + f"[{stamp}] {text.strip()}"
    conn.execute(
        "UPDATE items SET notes = ?, updated_at = ? WHERE id = ?",
        (new, utc_now(), item_id),
    )
    conn.commit()


def layout_set(conn, user_id: int, queue: list) -> None:
    conn.execute(
        "INSERT INTO layout_state (user_id, queue_json, pos, created_at) VALUES (?, ?, 0, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET queue_json = excluded.queue_json, pos = 0, created_at = excluded.created_at",
        (user_id, json.dumps(queue, ensure_ascii=False), utc_now()),
    )
    conn.commit()


def layout_get(conn, user_id: int):
    row = conn.execute("SELECT queue_json, pos FROM layout_state WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return None, 0
    try:
        return json.loads(row["queue_json"]), row["pos"]
    except json.JSONDecodeError:
        return None, 0


def layout_save_queue(conn, user_id: int, queue: list, pos: int) -> None:
    conn.execute(
        "UPDATE layout_state SET queue_json = ?, pos = ? WHERE user_id = ?",
        (json.dumps(queue, ensure_ascii=False), pos, user_id),
    )
    conn.commit()


def layout_clear(conn, user_id: int) -> None:
    conn.execute("DELETE FROM layout_state WHERE user_id = ?", (user_id,))
    conn.commit()


def find_item_by_words(conn, search: str):
    """Best fuzzy match of an item by space-separated keywords; None if too weak."""
    words = [w for w in re.split(r"\W+", (search or "").lower()) if len(w) >= 2]
    if not words:
        return None
    rows = conn.execute(
        "SELECT id, name, status, total_qty, available_qty, unit FROM items "
        "WHERE status NOT IN ('retired', 'lost')"
    ).fetchall()
    best, best_score = None, 0
    for row in rows:
        name_l = row["name"].lower()
        score = sum(1 for w in words if w in name_l)
        if score > best_score:
            best, best_score = row, score
    if best is not None and best_score >= max(1, (len(words) + 1) // 2):
        return best
    return None


def get_project(conn, project_id: str):
    return conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()


def project_items(conn, project_id: str):
    return conn.execute(
        "SELECT i.id, i.name, i.unit, i.category, u.qty FROM item_usage u JOIN items i ON i.id = u.item_id "
        "WHERE u.project_id = ? AND i.status != 'retired' ORDER BY i.category, i.name",
        (project_id,),
    ).fetchall()


def project_rename(conn, project_id: str, name: str) -> None:
    conn.execute(
        "UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
        (name.strip(), utc_now(), project_id),
    )
    conn.commit()


def project_delete(conn, project_id: str) -> int:
    """Удалить проект; его детали освобождаются на склад. Возвращает число деталей."""
    now = utc_now()
    rows = conn.execute("SELECT DISTINCT item_id FROM item_usage WHERE project_id = ?", (project_id,)).fetchall()
    freed = 0
    for r in rows:
        conn.execute("DELETE FROM item_usage WHERE project_id = ? AND item_id = ?", (project_id, r["item_id"]))
        left = conn.execute("SELECT COUNT(*) AS c FROM item_usage WHERE item_id = ?", (r["item_id"],)).fetchone()["c"]
        if not left:
            conn.execute(
                "UPDATE items SET status = 'stock', available_qty = total_qty, updated_at = ? WHERE id = ?",
                (now, r["item_id"]),
            )
        freed += 1
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    conn.commit()
    return freed


def project_remove_item(conn, project_id: str, item_id: str) -> None:
    """Вынуть деталь из проекта: позиция становится свободной."""
    now = utc_now()
    conn.execute("DELETE FROM item_usage WHERE project_id = ? AND item_id = ?", (project_id, item_id))
    left = conn.execute("SELECT COUNT(*) AS c FROM item_usage WHERE item_id = ?", (item_id,)).fetchone()["c"]
    if not left:
        conn.execute(
            "UPDATE items SET status = 'stock', available_qty = total_qty, updated_at = ? WHERE id = ?",
            (now, item_id),
        )
    conn.commit()


def project_set_desc(conn, project_id: str, text: str) -> None:
    conn.execute(
        "UPDATE projects SET description = ?, updated_at = ? WHERE id = ?",
        (text.strip(), utc_now(), project_id),
    )
    conn.commit()


def project_set_desc_msg(conn, project_id: str, msg_id: int) -> None:
    conn.execute("UPDATE projects SET desc_msg_id = ? WHERE id = ?", (msg_id, project_id))
    conn.commit()


def project_by_desc_msg(conn, msg_id: int):
    return conn.execute("SELECT * FROM projects WHERE desc_msg_id = ?", (msg_id,)).fetchone()


def item_set_in_project(conn, item_id: str, project_id: str) -> None:
    """Mark the whole position as living inside a project: usage record,
    status in_use, no free stock, verified now."""
    now = utc_now()
    row = conn.execute("SELECT total_qty FROM items WHERE id = ?", (item_id,)).fetchone()
    qty = row["total_qty"] if row else 1
    conn.execute("DELETE FROM item_usage WHERE item_id = ? AND project_id = ?", (item_id, project_id))
    conn.execute(
        "INSERT INTO item_usage (item_id, project_id, qty, role, since, removable) "
        "VALUES (?, ?, ?, '', ?, 1)",
        (item_id, project_id, qty, now[:10]),
    )
    conn.execute(
        "UPDATE items SET status = 'in_use', available_qty = 0, last_verified_at = ?, updated_at = ? WHERE id = ?",
        (now, now, item_id),
    )
    conn.commit()


def item_set_photo(conn, item_id: str, path: str) -> None:
    """Заменить все фото позиции одним новым."""
    conn.execute("DELETE FROM item_photos WHERE item_id = ?", (item_id,))
    conn.execute("INSERT OR IGNORE INTO item_photos (item_id, path) VALUES (?, ?)", (item_id, path))
    conn.commit()


def item_add_photo(conn, item_id: str, path: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO item_photos (item_id, path) VALUES (?, ?)",
        (item_id, path),
    )
    conn.commit()


def inv_mark_lost(conn, item_id: str) -> None:
    conn.execute(
        "UPDATE items SET total_qty = 0, available_qty = 0, status = 'lost', "
        "last_verified_at = ?, updated_at = ? WHERE id = ?",
        (utc_now(), utc_now(), item_id),
    )
    conn.commit()


def list_items_with_sources(conn, limit: int = 200):
    """Items with the first source URL (preferring kind='purchase')."""
    return conn.execute(
        """
        SELECT
            i.id, i.name, i.category, i.status, i.available_qty, i.total_qty,
            i.unit, i.location,
            (SELECT s.url FROM item_sources s
             WHERE s.item_id = i.id
             ORDER BY CASE s.kind WHEN 'purchase' THEN 0 ELSE 1 END, s.rowid
             LIMIT 1) AS source_url,
            (SELECT s.title FROM item_sources s
             WHERE s.item_id = i.id
             ORDER BY CASE s.kind WHEN 'purchase' THEN 0 ELSE 1 END, s.rowid
             LIMIT 1) AS source_title,
            (SELECT group_concat(p.name, ', ') FROM item_usage u JOIN projects p ON p.id = u.project_id
             WHERE u.item_id = i.id) AS projects_csv
        FROM items i
        WHERE i.status != 'retired' AND (i.total_qty > 0 OR i.status = 'in_use')
        ORDER BY i.category, i.name
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def list_projects(conn):
    return conn.execute("SELECT * FROM projects ORDER BY id").fetchall()


def get_proposal(conn, proposal_id: int):
    return conn.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()


def discard_proposal(conn, proposal_id: int) -> bool:
    cur = conn.execute(
        "UPDATE proposals SET status = 'discarded' WHERE id = ? AND status = 'pending'",
        (proposal_id,),
    )
    conn.commit()
    return cur.rowcount == 1


def remember_chat(conn, user_id: int, role: str, text: str) -> None:
    conn.execute(
        "INSERT INTO chat_messages (telegram_user_id, role, text, created_at) VALUES (?, ?, ?, ?)",
        (user_id, role, text or "", utc_now()),
    )
    conn.commit()


def recent_chat(conn, user_id: int, limit: int = 12):
    rows = conn.execute(
        """
        SELECT role, text
        FROM chat_messages
        WHERE telegram_user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    return list(reversed(rows))


def set_preference(conn, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO preferences (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, utc_now()),
    )
    conn.commit()


def get_preferences(conn) -> dict:
    rows = conn.execute("SELECT key, value FROM preferences ORDER BY key").fetchall()
    return {row["key"]: row["value"] for row in rows}


def next_item_id(conn) -> str:
    rows = conn.execute("SELECT id FROM items WHERE id LIKE 'hw-%'").fetchall()
    max_num = 0
    for row in rows:
        tail = row["id"].split("-")[-1]
        if tail.isdigit():
            max_num = max(max_num, int(tail))
    return f"hw-{datetime.now(timezone.utc).year}-{max_num + 1:03d}"


def apply_proposal(conn, proposal_id: int) -> dict:
    row = get_proposal(conn, proposal_id)
    if not row or row["status"] != "pending":
        raise ValueError("proposal is not pending")

    proposal = json.loads(row["proposal_json"])
    photo_paths = json.loads(row["photo_paths_json"])
    applied = []
    now = utc_now()

    for op in proposal.get("operations", []):
        op_name = op.get("op")
        if op_name == "ask_user":
            applied.append({"op": op_name, "result": "skipped"})
            continue

        item_id = op.get("item_id") or ""
        name = op.get("name") or "Unnamed item"
        qty = float(op.get("qty") or 0)
        unit = op.get("unit") or "pcs"
        location = op.get("location") or "unsorted"
        category = op.get("category") or "unknown"
        notes = op.get("notes") or ""
        status = op.get("status") or "stock"
        source_url = op.get("source_url") or ""
        source_title = op.get("source_title") or ""
        knowledge_summary = op.get("knowledge_summary") or ""

        if op_name == "add_item":
            if not item_id:
                item_id = next_item_id(conn)
            price_rub = float(op.get("price_rub") or 0)
            conn.execute(
                """
                INSERT OR IGNORE INTO items (
                  id, name, category, status, total_qty, available_qty, unit,
                  location, notes, price_rub, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item_id, name, category, status, qty, qty if status != "ordered" else 0, unit, location, notes, price_rub, now, now),
            )
            for path in photo_paths:
                conn.execute("INSERT OR IGNORE INTO item_photos (item_id, path) VALUES (?, ?)", (item_id, path))
            image_url = op.get("image_url") or ""
            if image_url and not photo_paths:
                conn.execute("INSERT OR IGNORE INTO item_photos (item_id, path) VALUES (?, ?)", (item_id, image_url))
            if source_url:
                conn.execute(
                    "INSERT OR IGNORE INTO item_sources (item_id, kind, title, url, notes) VALUES (?, 'purchase_or_reference', ?, ?, ?)",
                    (item_id, source_title, source_url, op.get("source_notes") or ""),
                )
            if knowledge_summary:
                conn.execute(
                    """
                    INSERT INTO item_knowledge (item_id, summary, specs_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(item_id) DO UPDATE SET summary = excluded.summary, specs_json = excluded.specs_json, updated_at = excluded.updated_at
                    """,
                    (item_id, knowledge_summary,
                     op.get("specs") if isinstance(op.get("specs"), str) and op.get("specs")
                     else json.dumps(op.get("specs") or {}, ensure_ascii=False),
                     now),
                )
            applied.append({"op": op_name, "item_id": item_id})

        elif op_name == "adjust_qty":
            if not item_id:
                raise ValueError("adjust_qty requires item_id")
            conn.execute(
                """
                UPDATE items
                SET total_qty = total_qty + ?,
                    available_qty = available_qty + ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (qty, qty, now, item_id),
            )
            applied.append({"op": op_name, "item_id": item_id, "qty": qty})

        elif op_name == "mark_used":
            if not item_id:
                raise ValueError("mark_used requires item_id")
            project_id = op.get("project_id") or "project-ideas-lab"
            conn.execute(
                """
                INSERT INTO item_usage (item_id, project_id, qty, role, since, removable)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (item_id, project_id, qty, notes, now[:10]),
            )
            conn.execute(
                "UPDATE items SET available_qty = available_qty - ?, status = 'in_use', updated_at = ? WHERE id = ?",
                (qty, now, item_id),
            )
            applied.append({"op": op_name, "item_id": item_id, "project_id": project_id, "qty": qty})

        elif op_name == "add_photo":
            if not item_id:
                raise ValueError("add_photo requires item_id")
            for path in photo_paths:
                conn.execute("INSERT OR IGNORE INTO item_photos (item_id, path) VALUES (?, ?)", (item_id, path))
            applied.append({"op": op_name, "item_id": item_id, "photos": len(photo_paths)})

    conn.execute("UPDATE proposals SET status = 'applied', applied_at = ? WHERE id = ?", (now, proposal_id))
    conn.commit()
    return {"proposal_id": proposal_id, "applied": applied}
