import json
import os
import re
import socket
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone


# Force IPv6-only resolution for hosts where IPv4 is slow/unreliable from this VPS.
# The provider's IPv4 path to api.telegram.org has 15s connect stalls; IPv6 is fine.
_IPV6_ONLY_HOSTS = {"api.telegram.org"}
_orig_getaddrinfo = socket.getaddrinfo


def _ipv6_first_getaddrinfo(host, *args, **kwargs):
    results = _orig_getaddrinfo(host, *args, **kwargs)
    if host in _IPV6_ONLY_HOSTS:
        v6 = [r for r in results if r[0] == socket.AF_INET6]
        if v6:
            return v6
    return results


socket.getaddrinfo = _ipv6_first_getaddrinfo

try:
    from .export_inventory import export_items, maybe_git_sync
    from .openai_chat import chat_reply, chat_reply_stream, classify_inv_intent, stock_rows_query as classify_stock_rows
    from .openai_extract import describe_photo, enrich_item, extract_device_layout, extract_inventory_proposal, scrape_amperkot
    from .storage import (
        apply_proposal,
        categories_with_counts,
        connect,
        discard_proposal,
        get_item,
        get_preferences,
        get_project,
        get_proposal,
        inv_clear_await,
        inv_events_for,
        inv_clear_pending,
        inv_finish,
        inv_get,
        inv_get_pending,
        inv_increment_seen,
        inv_log_event,
        inv_mark_lost,
        inv_mark_present,
        inv_mark_qty,
        inv_new_count,
        inv_next_item,
        inv_progress,
        inv_set_await,
        inv_set_current,
        inv_set_pass,
        inv_set_pending,
        inv_set_prompt_message,
        inv_set_skipped,
        inv_skipped,
        inv_start,
        item_add_photo,
        item_fetch_photo,
        item_set_photo,
        find_item_by_words,
        layout_clear,
        layout_get,
        layout_save_queue,
        layout_set,
        next_item_id,
        utc_now,
        items_in_category,
        item_append_note,
        item_projects,
        item_retire,
        project_by_desc_msg,
        project_create,
        project_delete,
        project_items,
        project_remove_item,
        project_rename,
        project_set_desc,
        project_set_desc_msg,
        item_apply_enrichment,
        item_set_category,
        item_set_name,
        item_set_price,
        item_set_source,
        item_set_in_project,
        item_split_to_project,
        item_first_photo,
        item_first_source,
        list_items,
        list_items_with_sources,
        list_pending,
        list_projects,
        recent_chat,
        remember_chat,
        save_proposal,
        seed_items_from_yaml,
        seed_projects,
        set_preference,
    )
except ImportError:
    from export_inventory import export_items, maybe_git_sync
    from openai_chat import chat_reply, chat_reply_stream, classify_inv_intent, stock_rows_query as classify_stock_rows
    from openai_extract import describe_photo, enrich_item, extract_device_layout, extract_inventory_proposal, scrape_amperkot
    from storage import (
        apply_proposal,
        categories_with_counts,
        connect,
        discard_proposal,
        get_item,
        get_preferences,
        get_project,
        get_proposal,
        inv_clear_await,
        inv_events_for,
        inv_clear_pending,
        inv_finish,
        inv_get,
        inv_get_pending,
        inv_increment_seen,
        inv_log_event,
        inv_mark_lost,
        inv_mark_present,
        inv_mark_qty,
        inv_new_count,
        inv_next_item,
        inv_progress,
        inv_set_await,
        inv_set_current,
        inv_set_pass,
        inv_set_pending,
        inv_set_prompt_message,
        inv_set_skipped,
        inv_skipped,
        inv_start,
        item_add_photo,
        item_fetch_photo,
        item_set_photo,
        find_item_by_words,
        layout_clear,
        layout_get,
        layout_save_queue,
        layout_set,
        next_item_id,
        utc_now,
        items_in_category,
        item_append_note,
        item_projects,
        item_retire,
        project_by_desc_msg,
        project_create,
        project_delete,
        project_items,
        project_remove_item,
        project_rename,
        project_set_desc,
        project_set_desc_msg,
        item_apply_enrichment,
        item_set_category,
        item_set_name,
        item_set_price,
        item_set_source,
        item_set_in_project,
        item_split_to_project,
        item_first_photo,
        item_first_source,
        list_items,
        list_items_with_sources,
        list_pending,
        list_projects,
        recent_chat,
        remember_chat,
        save_proposal,
        seed_items_from_yaml,
        seed_projects,
        set_preference,
    )


API_BASE = "https://api.telegram.org"
SITE_BASE = os.environ.get("ZAVHOZ_SITE_BASE", "https://azbezd.github.io/zavhoz-web")


def item_web_url(item_id: str) -> str:
    return f"{SITE_BASE}/item/{item_id}.html"


def _inventory_id_snapshot(conn, limit: int = 200) -> str:
    """Список позиций 'id | название | количество' — чтобы модель ссылалась на существующие."""
    rows = conn.execute(
        "SELECT id, name, available_qty, total_qty, unit FROM items WHERE status != 'retired' ORDER BY category, name LIMIT ?",
        (limit,),
    ).fetchall()
    lines = []
    for r in rows:
        q = f"{r['available_qty']:g}" if r["available_qty"] == r["total_qty"] else f"{r['available_qty']:g}/{r['total_qty']:g}"
        lines.append(f"{r['id']} | {r['name']} | {q} {r['unit'] or 'шт'}")
    return "\n".join(lines)


def token() -> str:
    value = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not value:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    return value


def allowed_user_ids() -> set[int]:
    raw = os.environ.get("ALLOWED_TELEGRAM_USER_IDS", "")
    return {int(part.strip()) for part in raw.split(",") if part.strip().isdigit()}


def telegram(method: str, payload: dict | None = None, timeout: int = 30) -> dict:
    data = None
    if payload is not None:
        data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(f"{API_BASE}/bot{token()}/{method}", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("ok", False):
        raise RuntimeError(f"telegram {method} returned ok=false: {body}")
    return body


def send(chat_id: int, text: str, parse_mode: str | None = None, disable_web_page_preview: bool = True,
         return_id: bool = False):
    payload = {"chat_id": chat_id, "text": text[:3900]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if disable_web_page_preview:
        payload["disable_web_page_preview"] = "true"
    body = telegram("sendMessage", payload)
    if return_id:
        return body.get("result", {}).get("message_id")
    return None


def send_html(chat_id: int, text: str) -> None:
    """Send a long HTML-formatted message, splitting on category boundaries
    (double newlines) if it doesn't fit in 3900 bytes."""
    chunks = []
    buf = ""
    for block in text.split("\n\n"):
        candidate = (buf + "\n\n" + block) if buf else block
        if len(candidate.encode("utf-8")) > 3900:
            if buf:
                chunks.append(buf)
            buf = block
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    for chunk in chunks:
        send(chat_id, chunk, parse_mode="HTML")


def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


CATEGORY_LABELS = {
    "computer": "🖥 Компьютеры и платформы",
    "microcontroller": "🧠 Микроконтроллеры",
    "module": "📦 Модули",
    "sensor": "📡 Сенсоры",
    "emitter": "💡 Излучатели (LED, дисплеи)",
    "semiconductor": "⚡️ Полупроводники",
    "passive": "🔘 Пассивные (резисторы, конденсаторы)",
    "connector": "🔌 Разъёмы",
    "wire": "🪢 Провода",
    "proto": "🟫 Платы прототипирования",
    "network": "🌐 Сетевое оборудование",
    "power": "🔋 Питание",
    "mechanical": "🔩 Механика",
    "tool": "🛠 Инструменты",
}


STATUS_LABELS = {
    "stock": "в наличии",
    "ordered": "ожидаю",
    "reserved": "зарезервировано",
    "in_use": "в проекте",
    "consumable": "расходник",
    "tool": "инструмент",
    "wishlist": "хочу купить",
    "lost": "потеряно",
    "retired": "списано",
}


def send_chat_action(chat_id: int, action: str = "typing") -> None:
    try:
        telegram("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=5)
    except Exception:
        pass


def send_photo(chat_id: int, photo_url: str, caption: str, reply_markup: dict | None = None) -> int | None:
    """Send a photo with a caption. Accepts an https URL, a Telegram file_id, or a
    path relative to the inventory repo (uploaded as multipart). Returns message_id."""
    if not photo_url.startswith("http") and ("/" in photo_url or photo_url.endswith(".jpg") or photo_url.endswith(".png")):
        repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
        return _send_photo_file(chat_id, os.path.join(repo_dir, photo_url), caption, reply_markup)
    payload = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption[:1000],
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        body = telegram("sendPhoto", payload, timeout=20)
        return body.get("result", {}).get("message_id")
    except Exception as exc:
        print(f"sendPhoto error: {exc}", flush=True)
        return None


def _item_qty_text(row) -> str:
    qty = row["available_qty"]
    total = row["total_qty"]
    projects_csv = row["projects_csv"] if "projects_csv" in row.keys() else None
    if projects_csv:
        return _fmt_qty(total, row["unit"])
    if qty == total:
        return _fmt_qty(qty, row["unit"])
    return f"свободно{NBSP}{_fmt_qty(qty, row['unit'])}{NBSP}из{NBSP}{_fmt_qty(total, row['unit'])}"


def _item_li(row) -> str:
    name = html_escape(_typo(row["name"]))
    # Имя ведёт на нашу страницу-карточку (не протухает); ссылка на магазин — внутри неё.
    name_part = f'<a href="{html_escape(item_web_url(row["id"]))}">{name}</a>'
    projects_csv = row["projects_csv"] if "projects_csv" in row.keys() else None
    proj_part = f"  ·  🔧{NBSP}{html_escape(projects_csv)}" if projects_csv else ""
    return f"<li>{name_part} — {_item_qty_text(row)}{proj_part}</li>"


def _render_stock_list(conn, chat_id: int, rows) -> None:
    """Склад одним сообщением: заголовки разделов + настоящие списки (Rich Messages).
    Откат — на цитатный вид по группам, если sendRichMessage недоступен."""
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row["category"] or "other", []).append(row)
    order = [c for c in CATEGORY_LABELS if c in groups] + [c for c in groups if c not in CATEGORY_LABELS]

    # Все группы свёрнуты — видны только заголовки, тап разворачивает нужную.
    parts = [f"<b>📋 Склад · {len(rows)} позиций · {len(order)} групп</b>"]
    for cat in order:
        items = groups[cat]
        label = CATEGORY_LABELS.get(cat, html_escape(cat))
        body = "<ul>" + "".join(_item_li(r) for r in items) + "</ul>"
        parts.append(f"<details><summary>{label} · {len(items)}</summary>{body}</details>")
    if send_rich(chat_id, "".join(parts)) is not None:
        return

    # Откат: старый вид — каждая группа отдельным сообщением, позиции цитатами.
    send(chat_id, f"📋 Склад: {len(rows)} позиций, {len(order)} групп.")
    for cat in order:
        items = groups[cat]
        label = CATEGORY_LABELS.get(cat, html_escape(cat))
        quotes = [f"<blockquote>{_item_li(r)[4:-5]}</blockquote>" for r in items]
        send(chat_id, f"<b>{label}</b> · {len(items)}\n" + "\n".join(quotes), parse_mode="HTML")


def send_rich(chat_id: int, html_content: str, reply_markup: dict | None = None) -> int | None:
    """Bot API 10.1 sendRichMessage: расширенный HTML (списки, заголовки, details)
    одним сообщением до 32768 символов. Возвращает message_id или None при сбое —
    тогда вызывающий откатывается на обычный send."""
    payload = {"chat_id": chat_id, "rich_message": json.dumps({"html": html_content}, ensure_ascii=False)}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        body = telegram("sendRichMessage", payload, timeout=20)
        return body.get("result", {}).get("message_id")
    except Exception as exc:
        print(f"sendRichMessage error: {exc}", flush=True)
        return None


def _send_photo_file(chat_id: int, abs_path: str, caption: str, reply_markup: dict | None = None) -> int | None:
    """Upload a local image file via multipart/form-data."""
    try:
        with open(abs_path, "rb") as fh:
            blob = fh.read()
    except OSError as exc:
        print(f"sendPhoto file error: {exc}", flush=True)
        return None
    boundary = "----zavhoz" + str(int(time.time() * 1000))
    parts = []

    def field(fname: str, value: str) -> None:
        parts.append(
            (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{fname}\"\r\n\r\n{value}\r\n").encode("utf-8")
        )

    field("chat_id", str(chat_id))
    field("caption", caption[:1000])
    field("parse_mode", "HTML")
    if reply_markup is not None:
        field("reply_markup", json.dumps(reply_markup, ensure_ascii=False))
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"photo.jpg\"\r\n"
         "Content-Type: image/jpeg\r\n\r\n").encode("utf-8") + blob + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(
        f"{API_BASE}/bot{token()}/sendPhoto",
        data=b"".join(parts),
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if body.get("ok"):
            return body.get("result", {}).get("message_id")
        print(f"sendPhoto upload not ok: {body}", flush=True)
    except Exception as exc:
        print(f"sendPhoto upload error: {exc}", flush=True)
    return None


def send_with_keyboard(chat_id: int, text: str, reply_markup: dict) -> int | None:
    payload = {
        "chat_id": chat_id,
        "text": text[:3900],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        "reply_markup": json.dumps(reply_markup, ensure_ascii=False),
    }
    try:
        body = telegram("sendMessage", payload)
        return body.get("result", {}).get("message_id")
    except Exception as exc:
        print(f"sendMessage(keyboard) error: {exc}", flush=True)
        return None


def edit_reply_markup(chat_id: int, message_id: int, reply_markup: dict | None) -> None:
    payload = {"chat_id": chat_id, "message_id": message_id}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    try:
        telegram("editMessageReplyMarkup", payload, timeout=10)
    except Exception as exc:
        print(f"editMessageReplyMarkup error: {exc}", flush=True)


def answer_callback(callback_query_id: str, text: str | None = None) -> None:
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text[:200]
    try:
        telegram("answerCallbackQuery", payload, timeout=5)
    except Exception:
        pass


def send_draft(chat_id: int, draft_id: int, text: str) -> bool:
    """Stream a partial message via Bot API 9.5 sendMessageDraft. Returns True on
    success. Drafts with the same draft_id animate; empty text shows a built-in
    'Thinking…' placeholder.
    """
    try:
        telegram(
            "sendMessageDraft",
            {"chat_id": chat_id, "draft_id": draft_id, "text": text[:3900]},
            timeout=10,
        )
        return True
    except Exception as exc:
        print(f"draft error: {exc}", flush=True)
        return False


def looks_like_inventory_update(text: str, has_photo: bool) -> bool:
    if has_photo:
        return True
    lowered = text.lower().strip()
    # Вопросы — это разговор со складом, а не его изменение.
    question_starts = ("сколько", "что ", "какие", "какой", "какая", "где", "есть ли",
                       "как ", "почему", "зачем", "покажи", "расскажи", "посоветуй")
    if "?" in lowered or lowered.startswith(question_starts):
        return False
    markers = [
        "купил",
        "купила",
        "нашёл",
        "нашел",
        "заказал",
        "пришло",
        "приехало",
        "добавь",
        "занеси",
        "использовал",
        "поставил",
        "потратил",
        "сломал",
        "потерял",
        "спиши",
        "списал",
        "выкинул",
        "выбросил",
    ]
    return any(marker in lowered for marker in markers)


def download_file(file_id: str, dest_dir: str) -> str:
    info = telegram("getFile", {"file_id": file_id})
    file_path = info["result"]["file_path"]
    url = f"{API_BASE}/file/bot{token()}/{file_path}"
    os.makedirs(dest_dir, exist_ok=True)
    ext = os.path.splitext(file_path)[1] or ".jpg"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    local_name = f"telegram-{stamp}-{file_id[-8:]}{ext}"
    repo_rel = os.path.join("photos", local_name)
    repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
    abs_path = os.path.join(repo_dir, repo_rel)
    with urllib.request.urlopen(url, timeout=60) as resp, open(abs_path, "wb") as fh:
        fh.write(resp.read())
    return repo_rel


def format_proposal(conn, proposal_id: int, proposal: dict) -> str:
    """Человекочитаемый черновик: для существующих позиций показываем имя и было→станет."""
    lines = [f"Черновик #{proposal_id}", proposal.get("summary", ""), ""]
    for idx, op in enumerate(proposal.get("operations", []), start=1):
        kind = op.get("op")
        cur = get_item(conn, op.get("item_id")) if op.get("item_id") else None
        unit = op.get("unit") or (cur["unit"] if cur else "шт")
        qty = op.get("qty") or 0
        if kind == "add_item":
            line = f"{idx}. ➕ Добавить: {op.get('name', '?')} — {qty:g} {unit}"
            if op.get("price_rub"):
                line += f", {op['price_rub']:g} ₽"
        elif kind == "update_item":
            tgt = cur["name"] if cur else (op.get("item_id") or "?")
            changes = []
            if op.get("name") and (not cur or op["name"] != cur["name"]):
                changes.append(f"имя → «{op['name']}»")
            if op.get("description"):
                changes.append("описание")
            if op.get("notes"):
                changes.append(f"заметка: {op['notes']}")
            line = f"{idx}. ✏️ Изменить «{tgt}»: " + (", ".join(changes) or "—")
        elif kind == "adjust_qty":
            if cur:
                line = f"{idx}. 🔢 {cur['name']}: было {cur['total_qty']:g} → станет {cur['total_qty'] + qty:g} {unit}"
            else:
                line = f"{idx}. 🔢 +{qty:g} {unit} (позиция не опознана!)"
        elif kind == "mark_used":
            tgt = cur["name"] if cur else (op.get("item_id") or "?")
            line = f"{idx}. 🔧 {tgt} → в проект {op.get('project_id', '?')} ({qty:g} {unit})"
        elif kind == "add_photo":
            line = f"{idx}. 📷 фото к {cur['name'] if cur else op.get('item_id', '?')}"
        elif kind == "ask_user":
            line = f"{idx}. ❓ {op.get('question', 'уточни')}"
        else:
            line = f"{idx}. {kind} | {op.get('name') or op.get('item_id') or '-'}"
        lines.append(line)
        if op.get("source_url"):
            lines.append(f"   источник: {op.get('source_title') or 'source'} — {op.get('source_url')}")
    lines.append("")
    lines.append("Применить кнопкой ниже, или ответь текстом — поправлю.")
    return "\n".join(lines)


def _send_proposal(conn, chat_id: int, proposal_id: int, proposal: dict) -> None:
    kb = {"inline_keyboard": [[
        {"text": "✅ Применить", "callback_data": f"prop:yes:{proposal_id}"},
        {"text": "🗑 Отменить", "callback_data": f"prop:no:{proposal_id}"},
    ]]}
    msg_id = send_with_keyboard(chat_id, html_escape(format_proposal(conn, proposal_id, proposal)), kb)
    if msg_id:
        conn.execute("UPDATE proposals SET draft_message_id = ? WHERE id = ?", (msg_id, proposal_id))
        conn.commit()


def _rebuild_draft(conn, chat_id: int, user_id: int, old_id: int, old_text: str, addition: str) -> None:
    """Пересобрать черновик с уточнением пользователя (правка без потери скрейпленных полей)."""
    send_chat_action(chat_id, "typing")
    combined = (old_text or "") + "\n\nУточнение от пользователя: " + addition
    try:
        proposal = extract_inventory_proposal(combined, [], _inventory_id_snapshot(conn))
    except Exception as exc:
        send(chat_id, f"Уточнение принял, но пересобрать черновик не вышло: {exc}")
        return
    # Скрейпленные поля (цена/фото/источник) не теряем при правке.
    try:
        old_ops = json.loads(conn.execute(
            "SELECT proposal_json FROM proposals WHERE id = ?", (old_id,)
        ).fetchone()["proposal_json"]).get("operations", [])
        donor = next((o for o in old_ops if o.get("op") == "add_item"
                      and (o.get("price_rub") or o.get("image_url"))), None)
        if donor:
            for op in proposal.get("operations", []):
                if op.get("op") == "add_item":
                    for f in ("price_rub", "image_url", "source_url", "source_title"):
                        if not op.get(f) and donor.get(f):
                            op[f] = donor[f]
    except Exception as exc:
        print(f"draft field carry-over error: {exc}", flush=True)
    ops = proposal.get("operations", [])
    if ops and all(o.get("op") == "ask_user" for o in ops):
        q = "; ".join(filter(None, (o.get("question") for o in ops))) or "Уточни, что именно сделать."
        send(chat_id, q)
        return
    discard_proposal(conn, old_id)
    new_id = save_proposal(conn, user_id, chat_id, combined, [], proposal)
    _send_proposal(conn, chat_id, new_id, proposal)


def _fresh_pending_draft(conn, user_id: int, minutes: int = 20):
    """Последний неприменённый черновик не старше minutes; иначе None."""
    pend = conn.execute(
        "SELECT id, message_text, created_at FROM proposals WHERE telegram_user_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    if not pend:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).replace(microsecond=0).isoformat()
    return pend if pend["created_at"] >= cutoff else None


def handle_command(conn, chat_id: int, user_id: int, text: str) -> None:
    parts = text.strip().split()
    cmd = parts[0].lower()
    repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())

    if cmd == "/start":
        msg = (
            "Я на связи. Я Завхоз: могу вести склад железок, разбирать фото заказов, "
            "помнить где что лежит и помогать по проектам.\n\n"
            "Пиши обычным языком: «купил 10 резисторов 220 Ом», «это ушло в FreeNetBox», "
            "«что у меня есть для ESP32?». Изменения склада сначала приходят черновиком "
            "с кнопками Применить/Отменить; ответом на черновик можно его поправить.\n\n"
            "📋 /list — склад по группам (нули и списанное скрыты, проектное — с пометкой)\n"
            "🔍 /inv — инвентаризация: всё управление кнопками (пропустить, к пропущенным, завершить)\n"
            "✏️ /edit — точечно изменить позицию из списка\n"
            "🛠 /projects — проекты\n\n"
            "📖 Витрина склада в вебе: " + SITE_BASE + "\n"
            "Экспорт и сайт обновляются автоматически после изменений."
        )
        send(chat_id, msg)
        remember_chat(conn, user_id, "assistant", msg)
    elif cmd == "/pending":
        rows = list_pending(conn)
        if not rows:
            send(chat_id, "Черновиков нет.")
            return
        lines = []
        for row in rows:
            proposal = json.loads(row["proposal_json"])
            lines.append(f"#{row['id']} {row['created_at']} — {proposal.get('summary', '')}")
        send(chat_id, "\n".join(lines))
    elif cmd == "/list":
        try:
            rows = list_items_with_sources(conn)
        except Exception:
            rows = list_items(conn)
        if not rows:
            send(chat_id, "Склад пока пустой.")
            return
        _render_stock_list(conn, chat_id, rows)
    elif cmd == "/projects":
        _projects_overview(conn, chat_id)
    elif cmd == "/enrich" and len(parts) == 2:
        item = get_item(conn, parts[1])
        if not item:
            send(chat_id, "Позиция не найдена. Укажи id вида hw-2026-001.")
        else:
            _enrich_new_items(conn, chat_id, {"applied": [{"op": "add_item", "item_id": item["id"]}]})
            _edit_export(conn, f"enrich {item['id']}")
    elif cmd in ("/newproject", "/newproj") and len(parts) >= 2:
        name = " ".join(parts[1:]).strip()
        pid, created = project_create(conn, name)
        _edit_export(conn, f"create project {pid}")
        send(chat_id, (f"🆕 Проект «{name}» создан." if created else f"Проект «{name}» уже есть."))
        _project_card(conn, chat_id, pid)
    elif cmd == "/show" and len(parts) == 2:
        row = get_proposal(conn, int(parts[1]))
        if not row:
            send(chat_id, "Proposal not found.")
            return
        send(chat_id, format_proposal(conn, row["id"], json.loads(row["proposal_json"])))
    elif cmd == "/discard" and len(parts) == 2:
        ok = discard_proposal(conn, int(parts[1]))
        send(chat_id, "Ок, выкинул черновик." if ok else "Такого активного черновика нет.")
    elif cmd == "/apply" and len(parts) == 2:
        result = apply_proposal(conn, int(parts[1]))
        send(chat_id, "Готово, внёс в склад.")
        _enrich_new_items(conn, chat_id, result)
        export_items(repo_dir)
        maybe_git_sync(repo_dir, f"apply telegram proposal {int(parts[1])}")
    elif cmd == "/export":
        export_items(repo_dir)
        send(chat_id, "Экспортировал inventory-файлы.")
    elif cmd == "/style" and len(parts) >= 2:
        value = " ".join(parts[1:])
        set_preference(conn, "style", value)
        send(chat_id, f"Запомнил стиль общения: {value}")
    elif cmd in ("/inv", "/inventory"):
        sess = inv_get(conn, user_id)
        if sess and sess["mode"] == "pick":
            inv_finish(conn, user_id)
            sess = None
        if sess:
            done, total_count = inv_progress(conn, user_id)
            kb = {"inline_keyboard": [[
                {"text": f"▶️ Продолжить ({total_count - done} осталось)", "callback_data": "inv:resume:_"},
                {"text": "🔄 Начать заново", "callback_data": "inv:restart:_"},
            ]]}
            send_with_keyboard(chat_id, f"Инвентаризация уже идёт: проверено {done} из {total_count}.", kb)
        else:
            new_count = inv_new_count(conn)
            if new_count:
                kb = {"inline_keyboard": [[
                    {"text": f"🆕 Только новые ({new_count})", "callback_data": "inv:scope:new"},
                    {"text": "🔍 Всё подряд", "callback_data": "inv:scope:all"},
                ]]}
                send_with_keyboard(chat_id, "Что проверяем?", kb)
            else:
                inv_start(conn, user_id, chat_id)
                send(chat_id, INV_INTRO)
                _inv_advance(conn, chat_id, user_id)
    elif cmd == "/edit":
        sess = inv_get(conn, user_id)
        if sess and sess["mode"] == "walk":
            send(chat_id, "Идёт инвентаризация — меняй позиции прямо в её карточках, "
                          "или заверши её (/stop_inv) и потом /edit.")
        else:
            if sess:
                inv_finish(conn, user_id)
            _pick_show_categories(conn, chat_id)
    elif cmd == "/skipped":
        sess = inv_get(conn, user_id)
        if not sess:
            send(chat_id, "Сессии инвентаризации нет. Начать — /inv.")
        else:
            skipped = inv_skipped(conn, user_id)
            if not skipped:
                send(chat_id, "Пропущенных позиций нет.")
            else:
                inv_set_pass(conn, user_id, 2)
                send(chat_id, f"Возвращаюсь к пропущенным ({len(skipped)}).")
                _inv_advance(conn, chat_id, user_id)
    elif cmd in ("/stop_inv", "/inv_stop"):
        if inv_get(conn, user_id):
            _inv_finish_with_summary(conn, chat_id, user_id)
        else:
            send(chat_id, "Сессии инвентаризации нет.")
    else:
        send(chat_id, "Не знаю такую команду. Можно просто написать обычным текстом, я разберусь.")


def _chat_with_stream(conn, chat_id: int, user_id: int, text: str) -> str:
    """Stream OpenAI Responses output into a Telegram message draft. Falls back to
    a plain synchronous chat_reply + sendMessage if drafts or streaming fail.
    Returns the final reply text. Always sends the final persisted sendMessage.
    """
    draft_id = int(time.time() * 1000) & 0x7FFFFFFF or 1
    drafts_alive = send_draft(chat_id, draft_id, "")
    if not drafts_alive:
        # Fallback A: no drafts → synchronous reply
        try:
            reply = chat_reply(conn, user_id, text, recent_chat(conn, user_id), get_preferences(conn))
        except Exception as exc:
            print(f"chat_reply FAILED: {type(exc).__name__}: {exc}", flush=True)
            reply = (
                "Я вижу сообщение, но AI-ответ сейчас не сработал.\n"
                f"Причина: {exc}\n\n"
                "Складовые команды без AI работают: /list, /projects, /pending."
            )
        send(chat_id, reply)
        return reply

    accumulated = ""
    last_push = time.monotonic()
    MIN_INTERVAL = 1.7  # seconds — stays under Telegram's edit rate limit
    MIN_DELTA = 20      # characters — avoid noise from tiny tokens

    try:
        for delta in chat_reply_stream(conn, user_id, text, recent_chat(conn, user_id), get_preferences(conn)):
            accumulated += delta
            now = time.monotonic()
            if now - last_push >= MIN_INTERVAL and len(accumulated) - 0 >= MIN_DELTA:
                if send_draft(chat_id, draft_id, accumulated):
                    last_push = now
                else:
                    # draft broke mid-stream — finish the stream, send plain
                    print("draft fell over mid-stream, will sendMessage at end", flush=True)
                    drafts_alive = False
    except Exception as exc:
        print(f"stream FAILED: {type(exc).__name__}: {exc}", flush=True)
        # Stream broke; if we have anything, send it; otherwise fall back to plain chat_reply
        if not accumulated.strip():
            try:
                accumulated = chat_reply(conn, user_id, text, recent_chat(conn, user_id), get_preferences(conn))
            except Exception as exc2:
                accumulated = (
                    "Я вижу сообщение, но AI-ответ сейчас не сработал.\n"
                    f"Причина: {exc2}\n\n"
                    "Складовые команды без AI работают: /list, /projects, /pending."
                )

    reply = accumulated.strip() or "Я не смог сформулировать ответ. Попробуй ещё раз чуть конкретнее."
    # Persist as a real, non-ephemeral message. The 30-second draft preview will fade on its own.
    send(chat_id, reply)
    return reply


NBSP = "\u00a0"  # неразрывный пробел

UNIT_LABELS = {"pcs": "шт", "m": "м", "meters": "м", "mm": "мм", "cm": "см", "g": "г", "kg": "кг"}

# Короткие предлоги/союзы: сшиваются со следующим словом неразрывным пробелом.
_TYPO_SMALL = ("в|во|без|до|из|к|на|по|о|об|обо|от|ото|с|со|у|за|над|под|про|при|"
               "и|а|но|да|или|же|ли|бы|не|ни|для")
_TYPO_RE = re.compile(r"(?:(?<=[\s(«\"])|^)(" + _TYPO_SMALL + r")[ ]+", re.IGNORECASE)


def _typo(text: str) -> str:
    """Лёгкая типографика отображаемых строк: предлоги и последнее слово не отрываются.
    К данным в БД не применяется — только к выводу."""
    if not text:
        return text
    t = _TYPO_RE.sub(lambda m: m.group(1) + NBSP, text)
    head, sep, tail = t.rpartition(" ")
    if sep and len(tail) <= 12:
        t = head + NBSP + tail
    return t


def _fmt_qty(qty, unit: str) -> str:
    """Число с десятичной запятой + единица через NBSP: «1,5 м», «10 шт»."""
    num = f"{qty:g}".replace(".", ",")
    return f"{num}{NBSP}{_unit_ru(unit)}"


def _unit_ru(unit: str) -> str:
    return UNIT_LABELS.get((unit or "pcs").lower(), unit or "шт")


def _inv_keyboard(item_id: str, skipped_count: int = 0, pass_no: int = 1) -> dict:
    rows = [
        [
            {"text": "✅ Есть", "callback_data": f"inv:ok:{item_id}"},
            {"text": "✏️ Изм.", "callback_data": f"inv:edit:{item_id}"},
        ],
        [
            {"text": "🔧 В проекте", "callback_data": f"inv:proj:{item_id}"},
            {"text": "⏭ Пропустить", "callback_data": f"inv:skip:{item_id}"},
        ],
    ]
    last_row = [{"text": "⏹ Завершить", "callback_data": "inv:stop:_"}]
    if pass_no == 1 and skipped_count:
        last_row.append({"text": f"↩️ Пропущенные ({skipped_count})", "callback_data": "inv:retskip:_"})
    rows.append(last_row)
    return {"inline_keyboard": rows}


def _enrich_new_items(conn, chat_id: int, apply_result: dict) -> None:
    """После применения черновика — обогатить новые позиции из открытых источников."""
    new_ids = [op.get("item_id") for op in (apply_result or {}).get("applied", [])
               if op.get("op") == "add_item" and op.get("item_id")]
    if not new_ids:
        return
    send_chat_action(chat_id, "typing")
    for iid in new_ids:
        item = get_item(conn, iid)
        if not item:
            continue
        try:
            enr = enrich_item(item["name"])
        except Exception as exc:
            print(f"enrich error {iid}: {exc}", flush=True)
            continue
        item_apply_enrichment(conn, iid, enr)
        # Нормальное фото товара из открытых источников (заменяет скриншот-опознание).
        if enr.get("image_url"):
            try:
                item_fetch_photo(conn, iid, enr["image_url"], os.environ.get("INVENTORY_REPO_DIR", os.getcwd()))
            except Exception as exc:
                print(f"enrich photo error {iid}: {exc}", flush=True)
        bits = []
        if enr.get("summary"):
            bits.append(enr["summary"])
        if enr.get("specs"):
            bits.append("Характеристики: " + ", ".join(f"{k}: {v}" for k, v in list(enr["specs"].items())[:6]))
        for d in (enr.get("docs") or [])[:3]:
            bits.append(f"📄 {d.get('title', 'Документация')}: {d['url']}")
        msg = f"🧠 Обогатил «{item['name']}» из открытых источников.\n" + ("\n".join(bits) if bits else "Доп. данных не нашёл.")
        msg += f"\n📖 {item_web_url(iid)}"
        send(chat_id, msg, disable_web_page_preview=True)


def _edit_export(conn, reason: str) -> None:
    """Тихий экспорт+пуш после точечной правки позиции."""
    repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
    try:
        export_items(repo_dir)
        maybe_git_sync(repo_dir, reason)
    except Exception as exc:
        print(f"edit export error: {exc}", flush=True)


def _inv_show_edit_menu(conn, chat_id: int, item) -> None:
    iid = item["id"]
    kb = {"inline_keyboard": [
        [
            {"text": "🔢 Количество", "callback_data": f"inv:qty:{iid}"},
            {"text": "🏷 Категория", "callback_data": f"inv:cat:{iid}"},
        ],
        [
            {"text": "✏️ Название", "callback_data": f"inv:name:{iid}"},
            {"text": "💰 Цена", "callback_data": f"inv:price:{iid}"},
        ],
        [
            {"text": "🔗 Источник", "callback_data": f"inv:source:{iid}"},
            {"text": "🗑 Списать", "callback_data": f"inv:retire:{iid}"},
        ],
        [{"text": "↩️ Отмена", "callback_data": "inv:pcancel:_"}],
    ]}
    send_with_keyboard(chat_id, f"Что меняем у «{html_escape(item['name'])}»?", kb)


def _inv_show_category_picker(conn, chat_id: int, item) -> None:
    rows, row = [], []
    for key, label in CATEGORY_LABELS.items():
        mark = "· " if key == item["category"] else ""
        row.append({"text": f"{mark}{label}", "callback_data": f"inv:cset:{item['id']}:{key}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "✍️ Своя — напишу текстом", "callback_data": f"inv:cnew:{item['id']}"}])
    rows.append([{"text": "↩️ Отмена", "callback_data": "inv:pcancel:_"}])
    cur = CATEGORY_LABELS.get(item["category"], item["category"] or "—")
    send_with_keyboard(chat_id, f"Категория для «{html_escape(item['name'])}» (сейчас: {cur}):",
                       {"inline_keyboard": rows})


def _inv_show_project_picker(conn, chat_id: int, item) -> None:
    rows, row = [], []
    for proj in list_projects(conn):
        row.append({"text": proj["name"], "callback_data": f"inv:pset:{item['id']}:{proj['id']}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "↩️ Отмена", "callback_data": "inv:pcancel:_"}])
    send_with_keyboard(chat_id, f"Где используется «{html_escape(item['name'])}»?", {"inline_keyboard": rows})


def _project_composition_rich(items) -> str:
    """Состав проекта по группам: заголовок категории + настоящий список."""
    if not items:
        return "<p><i>пока пусто</i></p>"
    groups: dict = {}
    for it in items:
        groups.setdefault(it["category"] or "other", []).append(it)
    order = [c for c in CATEGORY_LABELS if c in groups] + [c for c in groups if c not in CATEGORY_LABELS]
    out = []
    for cat in order:
        label = CATEGORY_LABELS.get(cat, html_escape(cat or "—"))
        out.append(f"<b>{label}</b><ul>")
        out.append("".join(
            f"<li>{html_escape(_typo(it['name']))} — {_fmt_qty(it['qty'], it['unit'])}</li>"
            for it in groups[cat]
        ))
        out.append("</ul>")
    return "".join(out)


def _projects_overview(conn, chat_id: int) -> None:
    projects = list_projects(conn)
    if not projects:
        send(chat_id, "Проекты пока не заведены.")
        return
    parts = [f"<b>🛠 Проекты · {len(projects)}</b>"]
    btn_row, rows = [], []
    for proj in projects:
        items = project_items(conn, proj["id"])
        desc = f" — <i>{html_escape(proj['description'])}</i>" if proj["description"] else ""
        summary = f"{html_escape(proj['name'])} · {len(items)} деталей"
        parts.append(f"<details><summary>{summary}</summary>"
                     + (f"<p><i>{html_escape(proj['description'])}</i></p>" if proj["description"] else "")
                     + _project_composition_rich(items) + "</details>")
        btn_row.append({"text": proj["name"], "callback_data": f"prj:show:{proj['id']}"})
        if len(btn_row) == 2:
            rows.append(btn_row)
            btn_row = []
    if btn_row:
        rows.append(btn_row)
    rows.append([{"text": "➕ Новый проект", "callback_data": "prj:new:_"}])
    parts.append("<p>Кнопкой ниже можно открыть проект и поправить его, либо создать новый.</p>")
    if send_rich(chat_id, "".join(parts), {"inline_keyboard": rows}) is not None:
        return
    # Откат на простой текст.
    flat = "<b>🛠 Проекты</b>\n" + "\n".join(
        f"<b>{html_escape(p['name'])}</b> — {len(project_items(conn, p['id']))} деталей" for p in projects)
    send_with_keyboard(chat_id, flat, {"inline_keyboard": rows})


def _project_card(conn, chat_id: int, project_id: str) -> None:
    proj = get_project(conn, project_id)
    if not proj:
        send(chat_id, "Проект не нашёл.")
        return
    items = project_items(conn, project_id)
    parts = [f"<b>🛠 {html_escape(proj['name'])}</b>"]
    if proj["description"]:
        parts.append(f"<p><i>{html_escape(proj['description'])}</i></p>")
    parts.append(_project_composition_rich(items))
    parts.append("<p>Ответь на это сообщение: текст станет описанием; "
                 "«переименуй в Имя» — переименует; «удали проект» — удалит.</p>")
    kb_rows = []
    if items:
        kb_rows.append([{"text": "➖ Вынуть деталь", "callback_data": f"prj:out:{project_id}"}])
    kb_rows.append([
        {"text": "🗑 Удалить проект", "callback_data": f"prj:del:{project_id}"},
        {"text": "↩️ Все проекты", "callback_data": "prj:list:_"},
    ])
    msg_id = send_rich(chat_id, "".join(parts), {"inline_keyboard": kb_rows})
    if msg_id is None:
        flat = [f"<b>🛠 {html_escape(proj['name'])}</b>"]
        if proj["description"]:
            flat.append(f"<i>{html_escape(proj['description'])}</i>")
        for it in items:
            flat.append(f"• {html_escape(it['name'])} — {_fmt_qty(it['qty'], it['unit'])}")
        msg_id = send_with_keyboard(chat_id, "\n".join(flat), {"inline_keyboard": kb_rows})
    if msg_id:
        project_set_desc_msg(conn, project_id, msg_id)


def _project_out_menu(conn, chat_id: int, project_id: str) -> None:
    proj = get_project(conn, project_id)
    items = project_items(conn, project_id)
    if not proj or not items:
        send(chat_id, "Вынимать нечего.")
        return
    rows = []
    for it in items[:30]:
        rows.append([{"text": f"{it['name'][:40]} ×{it['qty']:g}", "callback_data": f"prj:rm:{project_id}:{it['id']}"}])
    rows.append([{"text": "↩️ Отмена", "callback_data": f"prj:show:{project_id}"}])
    send_with_keyboard(chat_id, f"Что вынуть из «{html_escape(proj['name'])}»? Деталь станет свободной на складе.",
                       {"inline_keyboard": rows})


def _pick_show_categories(conn, chat_id: int) -> None:
    rows, row = [], []
    for cat in categories_with_counts(conn):
        label = CATEGORY_LABELS.get(cat["category"], cat["category"] or "—")
        row.append({"text": f"{label} · {cat['cnt']}", "callback_data": f"pick:cat:{cat['category']}:0"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    send_with_keyboard(chat_id, "✏️ Что меняем? Выбери группу:", {"inline_keyboard": rows})


def _pick_show_items(conn, chat_id: int, category: str, offset: int) -> None:
    items = items_in_category(conn, category, offset=offset, limit=8)
    has_more = len(items) > 8
    items = items[:8]
    rows = []
    for it in items:
        unit = _unit_ru(it["unit"])
        label = f"{it['name'][:38]} · {it['total_qty']:g}{NBSP}{unit}"
        rows.append([{"text": label, "callback_data": f"pick:item:{it['id']}"}])
    nav = []
    if offset > 0:
        nav.append({"text": "◂ Назад", "callback_data": f"pick:cat:{category}:{max(0, offset - 8)}"})
    if has_more:
        nav.append({"text": "Ещё ▸", "callback_data": f"pick:cat:{category}:{offset + 8}"})
    nav.append({"text": "↩️ Группы", "callback_data": "pick:cats:_"})
    rows.append(nav)
    label = CATEGORY_LABELS.get(category, category or "—")
    send_with_keyboard(chat_id, f"{label} — выбери позицию:", {"inline_keyboard": rows})


def _resolve_project(conn, text: str):
    """Match free-form project mention to a project row; None if ambiguous/unknown."""
    needle = (text or "").strip().lower()
    if not needle:
        return None
    projects = list_projects(conn)
    exact = [p for p in projects if p["name"].lower() == needle]
    if len(exact) == 1:
        return exact[0]
    partial = [p for p in projects if needle in p["name"].lower() or p["name"].lower() in needle]
    return partial[0] if len(partial) == 1 else None


def _inv_show_current(conn, chat_id: int, user_id: int) -> bool:
    """Pick next item and send it. Returns False if nothing left in the current pass."""
    item = inv_next_item(conn, user_id)
    if not item:
        return False
    _inv_show_item(conn, chat_id, user_id, item)
    return True


def _inv_show_item(conn, chat_id: int, user_id: int, item) -> None:
    sess = inv_get(conn, user_id)
    pass_no = sess["pass_no"] if sess else 1
    picking = bool(sess and sess["mode"] == "pick")
    skipped = inv_skipped(conn, user_id)
    done, total_count = inv_progress(conn, user_id)
    name = html_escape(item["name"])
    cat = CATEGORY_LABELS.get(item["category"], html_escape(item["category"] or "—"))
    qty = item["available_qty"]
    total = item["total_qty"]
    unit = html_escape(_unit_ru(item["unit"]))
    price = item["price_rub"] or 0
    src_title, src_url = item_first_source(conn, item["id"])
    qty_part = f"{qty:g}{NBSP}{unit}" if qty == total else f"{qty:g}/{total:g}{NBSP}{unit}"
    if picking:
        progress = "✏️ точечное изменение"
    else:
        progress = f"📋 {done + 1}{NBSP}из{NBSP}{total_count}"
        if pass_no >= 2:
            progress += "  ·  ↩️ второй круг"
    lines = [
        f"<i>{progress}</i>",
        f"<b>{name}</b>",
        "",
        f"🏷 {cat}",
        f"🔢 По базе: <b>{qty_part}</b>",
    ]
    proj_names = item_projects(conn, item["id"])
    if proj_names:
        lines.append(f"🔧 В проекте: {html_escape(', '.join(proj_names))}")
    if price:
        lines.append(f"💰 {price:g}{NBSP}₽")
    if src_url:
        lines.append(f'🔗 <a href="{html_escape(src_url)}">{html_escape(src_title or "источник")}</a>')
    lines.append(f'📖 <a href="{html_escape(item_web_url(item["id"]))}">карточка в вебе</a>')
    caption = "\n".join(lines)
    photo = item_first_photo(conn, item["id"])
    kb = _inv_keyboard(item["id"], skipped_count=len(skipped), pass_no=pass_no)
    if photo:
        msg_id = send_photo(chat_id, photo, caption, reply_markup=kb)
        if msg_id is None:
            msg_id = send_with_keyboard(chat_id, caption, kb)
    else:
        msg_id = send_with_keyboard(chat_id, caption, kb)
    if msg_id:
        inv_set_prompt_message(conn, user_id, msg_id)
    inv_set_current(conn, user_id, item["id"])


INV_INTRO = ("Начинаю инвентаризацию: иду от самых дорогих к самым дешёвым.\n\n"
             "Можно жать кнопки или отвечать словами: «да», «нет», «осталось 2», "
             "«есть, но не считал», «потом». Любой другой текст станет заметкой к позиции, "
             "фото — прикрепится к ней.")


def _inv_show_back(conn, chat_id: int, user_id: int) -> None:
    """Re-show the current card after a cancel/category change instead of moving on."""
    sess = inv_get(conn, user_id)
    if sess and sess["mode"] == "pick" and sess["current_item_id"]:
        item = get_item(conn, sess["current_item_id"])
        if item:
            _inv_show_item(conn, chat_id, user_id, item)
            return
    _inv_advance(conn, chat_id, user_id)


def _inv_advance(conn, chat_id: int, user_id: int) -> None:
    """Show the next card; switch to the skipped round when the main one ends;
    finish with a summary when nothing is left. In pick mode just close."""
    sess = inv_get(conn, user_id)
    if sess and sess["mode"] == "pick":
        inv_finish(conn, user_id)
        repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
        try:
            export_items(repo_dir)
            maybe_git_sync(repo_dir, "item edited via /edit")
        except Exception as exc:
            print(f"pick export error: {exc}", flush=True)
        send(chat_id, "Готово. Изменить ещё позицию — /edit.")
        return
    if _inv_show_current(conn, chat_id, user_id):
        return
    if sess and sess["pass_no"] == 1:
        skipped = inv_skipped(conn, user_id)
        if skipped:
            inv_set_pass(conn, user_id, 2)
            send(chat_id, f"Основной круг пройден. Возвращаюсь к пропущенным ({len(skipped)}).")
            if _inv_show_current(conn, chat_id, user_id):
                return
    _inv_finish_with_summary(conn, chat_id, user_id)


def _inv_finish_with_summary(conn, chat_id: int, user_id: int) -> None:
    sess = inv_get(conn, user_id)
    if not sess:
        send(chat_id, "Сессии инвентаризации нет.")
        return
    events = inv_events_for(conn, user_id)
    done, total_count = inv_progress(conn, user_id)
    uncounted = [e for e in events if e["action"] == "uncounted"]
    all_qty = [e for e in events if e["action"] == "qty"]
    # Числа без фактического изменения — это просто подтверждения.
    qty_changes = [e for e in all_qty if e["old_total"] != e["new_total"]]
    ok_count = sum(1 for e in events if e["action"] == "ok") + (len(all_qty) - len(qty_changes))
    lost = [e for e in events if e["action"] == "lost"]
    in_projects = [e for e in events if e["action"] == "project"]
    splits = [e for e in events if e["action"] == "split"]
    consumed = [e for e in events if e["action"] == "consumed"]
    unchecked = max(0, total_count - done)

    lines = [f"<b>Итог инвентаризации</b>  ·  проверено {done} из {total_count}"]
    if ok_count:
        lines.append(f"✅ Подтверждено: {ok_count}")
    if in_projects:
        lines.append("🔧 В проектах:")
        for e in in_projects:
            lines.append(f"  • {html_escape(e['item_name'])}")
    if splits:
        lines.append("🔀 Разделено (часть в проект):")
        for e in splits:
            lines.append(f"  • {html_escape(e['item_name'])}")
    if uncounted:
        lines.append(f"📦 Есть, без пересчёта: {len(uncounted)}")
    if qty_changes:
        lines.append("✏️ Изменено количество:")
        for e in qty_changes:
            old = f"{e['old_total']:g}" if e["old_total"] is not None else "?"
            new = f"{e['new_total']:g}" if e["new_total"] is not None else "?"
            lines.append(f"  • {html_escape(e['item_name'])}: {old} → {new}")
    if consumed or lost:
        lines.append("🗑 Списано (из учёта убрано):")
        for e in consumed + lost:
            lines.append(f"  • {html_escape(e['item_name'])}")
    if unchecked:
        lines.append(f"⏭ Не проверено: {unchecked} — всплывут при следующем /inv")
    inv_finish(conn, user_id)

    synced = ""
    if qty_changes or lost or in_projects or splits or consumed:
        repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
        try:
            export_items(repo_dir)
            maybe_git_sync(repo_dir, "inventory check via telegram /inv")
            if os.environ.get("INVENTORY_AUTO_GIT", "0") == "1":
                synced = "\n\n💾 Изменения выгружены в GitHub."
        except Exception as exc:
            print(f"inv export/sync error: {exc}", flush=True)
            synced = f"\n\n⚠️ Экспорт в GitHub не прошёл: {exc}"
    send_html(chat_id, "\n".join(lines) + synced)


def handle_callback_query(conn, callback: dict) -> None:
    cb_id = callback.get("id", "")
    user = callback.get("from", {}) or {}
    user_id = int(user.get("id", 0))
    msg = callback.get("message", {}) or {}
    chat = msg.get("chat", {}) or {}
    chat_id = int(chat.get("id", 0))
    chat_type = chat.get("type", "private")
    data = callback.get("data", "")

    if chat_type != "private":
        answer_callback(cb_id)
        return
    allowed = allowed_user_ids()
    if allowed and user_id not in allowed:
        answer_callback(cb_id)
        return

    if data.startswith("prj:"):
        answer_callback(cb_id)
        prj_rest = data[4:]
        if prj_rest == "list:_":
            _projects_overview(conn, chat_id)
        elif prj_rest == "new:_":
            msg_id = send(chat_id, "Ответь на это сообщение названием нового проекта.", return_id=True)
            if msg_id:
                set_preference(conn, "newproj_prompt_msg", str(msg_id))
        elif prj_rest.startswith("show:"):
            _project_card(conn, chat_id, prj_rest[5:])
        elif prj_rest.startswith("out:"):
            _project_out_menu(conn, chat_id, prj_rest[4:])
        elif prj_rest.startswith("del:"):
            pid = prj_rest[4:]
            proj = get_project(conn, pid)
            if proj:
                cnt = len(project_items(conn, pid))
                kb = {"inline_keyboard": [[
                    {"text": "🗑 Да, удалить", "callback_data": f"prj:delyes:{pid}"},
                    {"text": "↩️ Отмена", "callback_data": f"prj:show:{pid}"},
                ]]}
                send_with_keyboard(chat_id, f"Удалить проект «{html_escape(proj['name'])}»? "
                                            f"Его детали ({cnt}) вернутся на склад свободными.", kb)
        elif prj_rest.startswith("delyes:"):
            pid = prj_rest[7:]
            proj = get_project(conn, pid)
            if proj:
                freed = project_delete(conn, pid)
                repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
                try:
                    export_items(repo_dir)
                    maybe_git_sync(repo_dir, f"deleted project {pid}")
                except Exception as exc:
                    print(f"prj delete export error: {exc}", flush=True)
                send(chat_id, f"🗑 Проект «{proj['name']}» удалён, деталей освобождено: {freed}.")
                _projects_overview(conn, chat_id)
        elif prj_rest.startswith("rm:"):
            proj_id, _, iid = prj_rest[3:].partition(":")
            item = get_item(conn, iid)
            proj = get_project(conn, proj_id)
            if item and proj:
                project_remove_item(conn, proj_id, iid)
                repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
                try:
                    export_items(repo_dir)
                    maybe_git_sync(repo_dir, f"removed {iid} from {proj_id}")
                except Exception as exc:
                    print(f"prj export error: {exc}", flush=True)
                send(chat_id, f"➖ «{item['name']}» вынул из {proj['name']} — снова свободная.")
                _project_card(conn, chat_id, proj_id)
            else:
                send(chat_id, "Не нашёл позицию или проект.")
        return

    if data.startswith("prop:"):
        try:
            _, prop_act, prop_id_s = data.split(":")
            prop_id = int(prop_id_s)
        except ValueError:
            answer_callback(cb_id)
            return
        msg_id = msg.get("message_id")
        if msg_id:
            edit_reply_markup(chat_id, int(msg_id), None)
        repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
        if prop_act == "yes":
            answer_callback(cb_id, "Применяю")
            try:
                result = apply_proposal(conn, prop_id)
                send(chat_id, "Готово, внёс в склад.")
                _enrich_new_items(conn, chat_id, result)
                export_items(repo_dir)
                maybe_git_sync(repo_dir, f"apply telegram proposal {prop_id}")
            except Exception as exc:
                send(chat_id, f"Не получилось применить черновик #{prop_id}: {exc}")
        else:
            answer_callback(cb_id, "Отменил")
            discard_proposal(conn, prop_id)
            send(chat_id, f"Черновик #{prop_id} выкинул.")
        return

    if data.startswith("lay:"):
        lay_action = data.split(":")[1] if ":" in data else ""
        msg_id = msg.get("message_id")
        if msg_id:
            edit_reply_markup(chat_id, int(msg_id), None)
        if lay_action == "yes":
            answer_callback(cb_id, "Применяю")
            _layout_apply_current(conn, chat_id, user_id)
        elif lay_action == "skip":
            answer_callback(cb_id, "Пропустил")
            queue, pos = layout_get(conn, user_id)
            if queue is not None:
                layout_save_queue(conn, user_id, queue, pos + 1)
                _layout_show_next(conn, chat_id, user_id)
        elif lay_action == "stop":
            answer_callback(cb_id, "Остановил")
            layout_clear(conn, user_id)
            send(chat_id, "Раскладку остановил. Можно прислать описание заново в любой момент.")
        else:
            answer_callback(cb_id)
        return

    if data.startswith("pick:"):
        answer_callback(cb_id)
        rest = data[5:]
        if rest == "cats:_":
            _pick_show_categories(conn, chat_id)
        elif rest.startswith("cat:"):
            cat_part = rest[4:]
            category, _, off = cat_part.rpartition(":")
            _pick_show_items(conn, chat_id, category, int(off or 0))
        elif rest.startswith("item:"):
            item = get_item(conn, rest[5:])
            if not item:
                send(chat_id, "Позиция не найдена.")
                return
            sess = inv_get(conn, user_id)
            if sess and sess["mode"] == "walk":
                send(chat_id, "Идёт инвентаризация — сначала заверши её (/stop_inv).")
                return
            inv_start(conn, user_id, chat_id, mode="pick")
            _inv_show_item(conn, chat_id, user_id, item)
        return

    if not data.startswith("inv:"):
        answer_callback(cb_id)
        return

    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    item_id = parts[2] if len(parts) > 2 else ""

    # Выбор охвата — сессии ещё нет, обрабатываем до проверки.
    if action == "scope":
        scope = "new" if item_id == "new" else "all"
        answer_callback(cb_id, "Только новые" if scope == "new" else "Всё подряд")
        msg_id = msg.get("message_id")
        if msg_id:
            edit_reply_markup(chat_id, int(msg_id), None)
        inv_start(conn, user_id, chat_id, scope=scope)
        send(chat_id, INV_INTRO if scope == "all" else "Иду только по новым (непроверенным) позициям, от дорогих к дешёвым.")
        _inv_advance(conn, chat_id, user_id)
        return

    sess = inv_get(conn, user_id)
    if not sess:
        answer_callback(cb_id, "Сессия инвентаризации не запущена. Команда /inv.")
        return

    # Снимаем клавиатуру у предыдущего сообщения
    msg_id = msg.get("message_id")
    if msg_id:
        edit_reply_markup(chat_id, int(msg_id), None)

    if action == "stop":
        answer_callback(cb_id, "Завершаю")
        _inv_finish_with_summary(conn, chat_id, user_id)
        return
    if action == "pause":
        answer_callback(cb_id, "Пауза")
        done, total_count = inv_progress(conn, user_id)
        send(chat_id, f"Поставил на паузу: проверено {done} из {total_count}. Продолжить — /inv.")
        return
    if action == "resume":
        answer_callback(cb_id, "Продолжаю")
        _inv_advance(conn, chat_id, user_id)
        return
    if action == "restart":
        answer_callback(cb_id, "Начинаю заново")
        inv_start(conn, user_id, chat_id)
        _inv_advance(conn, chat_id, user_id)
        return
    if action == "retskip":
        skipped = inv_skipped(conn, user_id)
        if not skipped:
            answer_callback(cb_id, "Пропущенных нет")
            _inv_advance(conn, chat_id, user_id)
            return
        answer_callback(cb_id, "К пропущенным")
        inv_set_pass(conn, user_id, 2)
        send(chat_id, f"Возвращаюсь к пропущенным ({len(skipped)}).")
        _inv_advance(conn, chat_id, user_id)
        return
    if action == "qcancel":
        inv_clear_await(conn, user_id)
        answer_callback(cb_id, "Отменил")
        _inv_show_back(conn, chat_id, user_id)
        return
    if action == "cyes":
        pending = inv_get_pending(conn, user_id)
        inv_clear_pending(conn, user_id)
        if not pending:
            answer_callback(cb_id, "Нечего подтверждать")
            return
        item = get_item(conn, pending.get("item_id", ""))
        if not item:
            answer_callback(cb_id, "Позиция не найдена")
            return
        answer_callback(cb_id, "Применяю")
        _inv_apply_action(conn, chat_id, user_id, item, pending.get("action", ""),
                          qty=pending.get("qty"), note=pending.get("note"),
                          extra_assignments=pending.get("assignments"))
        return
    if action == "cno":
        inv_clear_pending(conn, user_id)
        answer_callback(cb_id, "Отменил")
        send(chat_id, "Ок, не записал. Поясни словами или нажми кнопку под карточкой.")
        return
    if action == "pcancel":
        answer_callback(cb_id, "Отменил")
        _inv_show_back(conn, chat_id, user_id)
        return
    if action == "pset" and item_id:
        iid, _, proj_id = item_id.partition(":")
        item = get_item(conn, iid)
        proj = get_project(conn, proj_id)
        if not item or not proj:
            answer_callback(cb_id, "Не нашёл позицию или проект")
            return
        item_set_in_project(conn, iid, proj_id)
        inv_increment_seen(conn, user_id)
        inv_log_event(conn, user_id, iid, f"{item['name']} → {proj['name']}", "project")
        answer_callback(cb_id, f"В проекте {proj['name']}")
        send(chat_id, f"🔧 {item['name']} — в проекте {proj['name']}.")
        _inv_advance(conn, chat_id, user_id)
        return
    if action == "cset" and item_id:
        iid, _, cat_key = item_id.partition(":")
        item = get_item(conn, iid)
        if not item:
            answer_callback(cb_id, "Не нашёл позицию")
            return
        item_set_category(conn, iid, cat_key)
        label = CATEGORY_LABELS.get(cat_key, cat_key)
        answer_callback(cb_id, "Категория обновлена")
        send(chat_id, f"🏷 {item['name']} → {label}.")
        _inv_show_back(conn, chat_id, user_id)
        return
    if action == "cnew" and item_id:
        item = get_item(conn, item_id)
        if not item:
            answer_callback(cb_id, "Не нашёл позицию")
            return
        inv_set_await(conn, user_id, item_id, kind="cat")
        answer_callback(cb_id, "Жду название")
        send(chat_id, "Напиши название новой категории одним сообщением.")
        return

    item = get_item(conn, item_id) if item_id else None
    if action == "proj" and item:
        answer_callback(cb_id)
        _inv_show_project_picker(conn, chat_id, item)
        return
    if action == "edit" and item:
        answer_callback(cb_id)
        _inv_show_edit_menu(conn, chat_id, item)
        return
    if action == "cat" and item:
        answer_callback(cb_id)
        _inv_show_category_picker(conn, chat_id, item)
        return
    if action in ("name", "price", "source") and item:
        prompts = {
            "name": "Напиши новое название одним сообщением.",
            "price": "Напиши новую цену в рублях (число).",
            "source": "Пришли ссылку на источник (магазин/карточку).",
        }
        inv_set_await(conn, user_id, item_id, kind=action)
        answer_callback(cb_id, "Жду ввод")
        send(chat_id, prompts[action])
        return
    if action == "retire" and item:
        answer_callback(cb_id)
        kb = {"inline_keyboard": [[
            {"text": "🗑 Да, списать", "callback_data": f"inv:retireyes:{item_id}"},
            {"text": "↩️ Отмена", "callback_data": "inv:pcancel:_"},
        ]]}
        send_with_keyboard(chat_id, f"Списать «{html_escape(item['name'])}»? Уйдёт из учёта.", kb)
        return
    if action == "retireyes" and item:
        item_retire(conn, item_id)
        answer_callback(cb_id, "Списано")
        send(chat_id, f"🗑 {item['name']} — списал, из учёта убрано.")
        repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
        try:
            export_items(repo_dir)
            maybe_git_sync(repo_dir, f"retire {item_id} via edit")
        except Exception as exc:
            print(f"retire export error: {exc}", flush=True)
        _inv_advance(conn, chat_id, user_id)
        return
    if action == "ok" and item:
        inv_mark_present(conn, item_id)
        inv_increment_seen(conn, user_id)
        inv_log_event(conn, user_id, item_id, item["name"], "ok")
        answer_callback(cb_id, "Отмечено: всё на месте")
    elif action == "lost" and item:
        inv_log_event(conn, user_id, item_id, item["name"], "consumed", old_total=item["total_qty"])
        item_retire(conn, item_id)
        inv_increment_seen(conn, user_id)
        answer_callback(cb_id, "Списано")
    elif action == "uncnt" and item:
        inv_mark_present(conn, item_id)
        inv_clear_await(conn, user_id)
        inv_increment_seen(conn, user_id)
        inv_log_event(conn, user_id, item_id, item["name"], "uncounted")
        answer_callback(cb_id, "Есть, без пересчёта")
    elif action == "skip" and item:
        if sess["pass_no"] >= 2:
            # Второй пропуск: оставляем непроверенной, выпадет в следующей инвентаризации.
            skipped = [sid for sid in inv_skipped(conn, user_id) if sid != item_id]
            inv_set_skipped(conn, user_id, skipped)
            answer_callback(cb_id, "Оставил непроверенной")
        else:
            skipped = inv_skipped(conn, user_id)
            if item_id not in skipped:
                skipped.append(item_id)
            inv_set_skipped(conn, user_id, skipped)
            answer_callback(cb_id, "Отложил на потом")
    elif action in ("qty", "less") and item:
        inv_set_await(conn, user_id, item_id)
        answer_callback(cb_id, "Жду число")
        unit = _unit_ru(item["unit"])
        kb = {"inline_keyboard": [[
            {"text": "📦 Есть, не считал", "callback_data": f"inv:uncnt:{item_id}"},
            {"text": "↩️ Отмена", "callback_data": "inv:qcancel:_"},
        ]]}
        send_with_keyboard(
            chat_id,
            f"Сколько по факту? Сейчас по базе {item['total_qty']:g}{NBSP}{unit}. "
            f"Напиши число (можно с точкой, например 2.5).",
            kb,
        )
        return
    else:
        answer_callback(cb_id)
        return

    # Дальше — следующая позиция, второй круг или финал
    _inv_advance(conn, chat_id, user_id)


_NUM_RE = re.compile(
    r"^(\d+(?:[.,]\d+)?)\s*(шт|штук|штуки|штука|м|метр|метра|метров|мм|см|г|кг|pcs|m)?\.?$",
    re.IGNORECASE,
)

_INV_RULES = {
    "ok": {"да", "ок", "ok", "есть", "на месте", "всё на месте", "все на месте", "всё ок",
           "все ок", "+", "есть всё", "есть все", "всё есть", "все есть"},
    "lost": {"нет", "нету", "потерял", "потеряно", "не нашёл", "не нашел", "нигде нет"},
    "skip": {"пропусти", "пропустить", "потом", "дальше", "скип", "следующая", "следующий"},
    "stop": {"стоп", "хватит", "закончи", "закончить", "завершить", "завершай", "конец"},
    "pause": {"пауза", "перерыв", "продолжим позже", "продолжу позже"},
    "uncounted": {"не считал", "не считала", "есть но не считал", "есть, но не считал",
                  "не знаю сколько", "хз сколько", "не знаю"},
    "consumed": {"списал", "списано", "спиши", "израсходовал", "израсходовано",
                 "выкинул", "выбросил", "использовал все", "использовал всё", "кончились", "кончился"},
}


def _parse_qty(text: str):
    m = _NUM_RE.match(text.strip().lower())
    return float(m.group(1).replace(",", ".")) if m else None


def _rule_intent(text: str):
    t = text.strip().lower().rstrip("!.…)")
    for action, phrases in _INV_RULES.items():
        if t in phrases:
            return action
    return None


def _inv_apply_action(conn, chat_id: int, user_id: int, item, action: str,
                      qty=None, note: str | None = None, extra_assignments=None) -> None:
    """Apply a verification outcome expressed in free text to the current card."""
    item_id = item["id"]
    name = item["name"]
    # Для in_project параметр note несёт название проекта, а не заметку.
    if note and action != "in_project":
        item_append_note(conn, item_id, note)
    noted = " Заметку записал." if note and action != "in_project" else ""

    if action == "ok":
        inv_mark_present(conn, item_id)
        inv_clear_await(conn, user_id)
        inv_increment_seen(conn, user_id)
        inv_log_event(conn, user_id, item_id, name, "ok")
        send(chat_id, f"✅ {name} — на месте.{noted}")
        _inv_advance(conn, chat_id, user_id)
    elif action == "lost":
        # «Нет/потерял» = списание: из учёта исчезает насовсем.
        inv_log_event(conn, user_id, item_id, name, "consumed", old_total=item["total_qty"])
        item_retire(conn, item_id)
        inv_clear_await(conn, user_id)
        inv_increment_seen(conn, user_id)
        send(chat_id, f"🗑 {name} — списал, из учёта убрано.{noted}")
        _inv_advance(conn, chat_id, user_id)
    elif action == "uncounted":
        inv_mark_present(conn, item_id)
        inv_clear_await(conn, user_id)
        inv_increment_seen(conn, user_id)
        inv_log_event(conn, user_id, item_id, name, "uncounted")
        send(chat_id, f"📦 {name} — есть, без точного пересчёта.{noted}")
        _inv_advance(conn, chat_id, user_id)
    elif action == "qty" and qty is not None:
        old_total = item["total_qty"]
        inv_mark_qty(conn, item_id, qty)
        inv_clear_await(conn, user_id)
        inv_increment_seen(conn, user_id)
        inv_log_event(conn, user_id, item_id, name, "qty", old_total=old_total, new_total=qty)
        unit = _unit_ru(item["unit"])
        send(chat_id, f"✏️ {name}: записал {qty:g}{NBSP}{unit} (было {old_total:g}).{noted}")
        _inv_advance(conn, chat_id, user_id)
    elif action == "in_project":
        proj = _resolve_project(conn, note or "")
        if proj:
            item_set_in_project(conn, item_id, proj["id"])
            inv_clear_await(conn, user_id)
            inv_increment_seen(conn, user_id)
            inv_log_event(conn, user_id, item_id, f"{name} → {proj['name']}", "project")
            send(chat_id, f"🔧 {name} — в проекте {proj['name']}.")
            _inv_advance(conn, chat_id, user_id)
        else:
            _inv_show_project_picker(conn, chat_id, item)
    elif action == "consumed":
        item_retire(conn, item_id)
        inv_clear_await(conn, user_id)
        inv_increment_seen(conn, user_id)
        inv_log_event(conn, user_id, item_id, name, "consumed", old_total=item["total_qty"])
        send(chat_id, f"🗑 {name} — списал, из учёта убрано.{noted}")
        _inv_advance(conn, chat_id, user_id)
    elif action == "split" and qty is not None:
        # qty здесь = сколько ушло в проект; note = название проекта (выверено заранее).
        proj = _resolve_project(conn, note or "")
        if not proj:
            send(chat_id, "Не понял, в какой проект ушла часть. Назови проект: FreeNet, FreeNetBox, NetBox, DachaNetBox.")
            return
        new_id, remaining = item_split_to_project(conn, item_id, qty, proj["id"])
        inv_clear_await(conn, user_id)
        inv_increment_seen(conn, user_id)
        inv_log_event(conn, user_id, item_id, f"{name}: {qty:g} → {proj['name']}, свободно {remaining:g}", "split")
        unit = _unit_ru(item["unit"])
        send(chat_id, f"🔀 {name}: {qty:g}{NBSP}{unit} → {proj['name']} (новая позиция в списке), "
                      f"свободно осталось {remaining:g}{NBSP}{unit}.")
        _inv_advance(conn, chat_id, user_id)
    elif action == "multi" and extra_assignments:
        unit = _unit_ru(item["unit"])
        applied = []
        remaining = item["total_qty"]
        for a in extra_assignments:
            proj = _resolve_project(conn, a["project"])
            if not proj:
                send(chat_id, f"Проект «{a['project']}» не нашёл — раскладку не применил. Уточни название.")
                return
            _, remaining = item_split_to_project(conn, item_id, a["qty"], proj["id"])
            inv_log_event(conn, user_id, item_id, f"{name}: {a['qty']:g} → {proj['name']}", "split")
            applied.append(f"{a['qty']:g}{NBSP}{unit} → {proj['name']}")
        inv_clear_await(conn, user_id)
        inv_increment_seen(conn, user_id)
        send(chat_id, f"🔀 {name}: " + "; ".join(applied) + f". Свободно осталось {remaining:g}{NBSP}{unit}.")
        _inv_advance(conn, chat_id, user_id)
    elif action == "skip":
        sess = inv_get(conn, user_id)
        inv_clear_await(conn, user_id)
        if sess and sess["pass_no"] >= 2:
            skipped = [sid for sid in inv_skipped(conn, user_id) if sid != item_id]
            inv_set_skipped(conn, user_id, skipped)
            send(chat_id, f"⏭ {name} — оставил непроверенной.{noted}")
        else:
            skipped = inv_skipped(conn, user_id)
            if item_id not in skipped:
                skipped.append(item_id)
            inv_set_skipped(conn, user_id, skipped)
            send(chat_id, f"⏭ {name} — отложил, вернёмся в конце или по /skipped.{noted}")
        _inv_advance(conn, chat_id, user_id)
    elif action == "stop":
        _inv_finish_with_summary(conn, chat_id, user_id)
    elif action == "pause":
        done, total_count = inv_progress(conn, user_id)
        send(chat_id, f"Поставил на паузу: проверено {done} из {total_count}. Продолжить — /inv.")
    elif action == "note":
        send(chat_id, f"📝 Записал заметку к «{name}». Жду ответ по наличию — кнопкой или словом («есть», «нет», «осталось 2»).")
    else:
        send(chat_id, "Не понял. Нажми кнопку под карточкой или напиши число/«есть»/«нет».")


def _add_from_amperkot(conn, chat_id: int, user_id: int, url: str, qty: int = 1) -> None:
    """Завести позицию по карточке amperkot: имя/цена/фото со страницы, потом черновик."""
    data = scrape_amperkot(url)
    name = data["name"] or "Товар с amperkot"
    proposal = {
        "summary": f"Добавить с amperkot: {name}",
        "operations": [{
            "op": "add_item", "item_id": "", "name": name, "category": "module",
            "qty": qty, "unit": "pcs", "location": "unsorted", "project_id": "",
            "notes": "", "question": "", "status": "stock",
            "source_title": "amperkot.ru", "source_url": url, "source_notes": "",
            "knowledge_summary": "", "specs": "{}", "confidence": "high",
            "price_rub": data["price"], "image_url": data["image"],
        }],
    }
    proposal_id = save_proposal(conn, user_id, chat_id, f"amperkot: {url}", [], proposal)
    if data["image"]:
        try:
            send_photo(chat_id, data["image"], f"<b>{html_escape(name)}</b>\n💰 {data['price'] or '—'}{NBSP}₽")
        except Exception:
            pass
    remember_chat(conn, user_id, "assistant", proposal["summary"])
    _send_proposal(conn, chat_id, proposal_id, proposal)


def _looks_like_stock_question(text: str) -> bool:
    low = text.lower().strip()
    starts = ("сколько", "какие", "какой", "какая", "что есть", "что у меня", "есть ли",
              "что за", "найди", "покажи", "есть у", "имеется")
    if low.startswith(starts):
        return True
    # «<деталь> есть?», «… в наличии?», «… имеется?» — вопрос о наличии в любой форме.
    cues = ("есть ли", "у нас есть", "у меня есть", "в наличии", "имеется")
    if any(c in low for c in cues):
        return True
    stripped = low.rstrip("?!. )")
    return stripped.endswith(("есть", "имеется", "в наличии", "остался", "осталось", "остались"))


def _answer_stock_question(conn, chat_id: int, text: str) -> bool:
    """Вопрос о наличии: AI выбирает строки склада, бот рендерит список с количествами.
    Возвращает True, если ответ отправлен."""
    try:
        rows, comment, items = classify_stock_rows(conn, text)
    except Exception as exc:
        print(f"stock query error: {type(exc).__name__}: {exc}", flush=True)
        return False
    sel = [items[r - 1] for r in rows if 1 <= r <= len(items)]
    if not sel:
        return False
    total_sum, units = 0.0, set()
    for row in sel:
        projects_csv = row["projects_csv"] if "projects_csv" in row.keys() else None
        total_sum += (row["total_qty"] if projects_csv else row["available_qty"]) or 0
        units.add(_unit_ru(row["unit"]))
    head = f"Нашёл позиций: {len(sel)}"
    if comment:
        head += f" — {html_escape(comment)}"
    tail = ""
    if len(sel) > 1 and len(units) == 1:
        tail = f"\n<b>Итого: {_fmt_qty(total_sum, sel[0]['unit'])}</b>"

    def _stock_li(row):
        # Имя + количество + явная ссылка на карточку (чтобы «дай ссылку» было сразу выполнено).
        name = html_escape(_typo(row["name"]))
        projects_csv = row["projects_csv"] if "projects_csv" in row.keys() else None
        proj = f"  ·  🔧{NBSP}{html_escape(projects_csv)}" if projects_csv else ""
        url = item_web_url(row["id"])
        return (f'<li>{name} — {_item_qty_text(row)}{proj}  ·  '
                f'<a href="{html_escape(url)}">📖 карточка</a></li>')

    rich = f"<b>{head}</b><ul>" + "".join(_stock_li(r) for r in sel) + "</ul>" + (f"<p>{tail.strip()}</p>" if tail else "")
    if send_rich(chat_id, rich) is not None:
        return True
    # Откат: текст + явные ссылки.
    lines = [head]
    for r in sel:
        lines.append(f"• {_typo(r['name'])} — {_item_qty_text(r)}\n  📖 {item_web_url(r['id'])}")
    send(chat_id, "\n".join(lines) + tail.replace("<b>", "").replace("</b>", ""))
    return True


def _looks_like_device_layout(conn, text: str) -> bool:
    """Описание устройств с деталями: упомянуты проекты, это не вопрос."""
    if "?" in text:
        return False
    lowered = text.lower()
    hits = sum(1 for p in list_projects(conn) if p["name"].lower() in lowered)
    return hits >= 2 or (hits >= 1 and len(text) > 80)


def _layout_start(conn, chat_id: int, user_id: int, text: str) -> None:
    send_chat_action(chat_id, "typing")
    project_names = [p["name"] for p in list_projects(conn)]
    try:
        items = extract_device_layout(text, project_names)
    except Exception as exc:
        print(f"layout extract error: {exc}", flush=True)
        send(chat_id, f"Не смог разобрать описание устройств: {exc}")
        return
    if not items:
        send(chat_id, "Не нашёл в тексте деталей для раскладки.")
        return
    queue = []
    for it in items:
        match = find_item_by_words(conn, it.get("search") or it.get("name", ""))
        queue.append({
            "project": it.get("project", ""),
            "name": it.get("name", "?"),
            "qty": float(it.get("qty") or 1),
            "uncertain": bool(it.get("uncertain")),
            "note": it.get("note", ""),
            "matched_id": match["id"] if match else "",
            "matched_name": match["name"] if match else "",
        })
    layout_set(conn, user_id, queue)
    send(chat_id, f"Разобрал описание: {len(queue)} пунктов. Пройдём по одному — подтверждай.")
    _layout_show_next(conn, chat_id, user_id)


def _layout_show_next(conn, chat_id: int, user_id: int) -> None:
    queue, pos = layout_get(conn, user_id)
    if queue is None:
        return
    if pos >= len(queue):
        applied = sum(1 for e in queue if e.get("done") == "yes")
        created = sum(1 for e in queue if e.get("created"))
        skipped = len(queue) - applied
        layout_clear(conn, user_id)
        repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
        synced = ""
        if applied:
            try:
                export_items(repo_dir)
                maybe_git_sync(repo_dir, "device layout via telegram")
                if os.environ.get("INVENTORY_AUTO_GIT", "0") == "1":
                    synced = " Изменения выгружены в GitHub."
            except Exception as exc:
                print(f"layout export error: {exc}", flush=True)
        send(chat_id, f"Раскладка готова: применено {applied}, из них создано новых {created}, пропущено {skipped}.{synced}")
        return
    entry = queue[pos]
    proj_part = f" → {entry['project']}" if entry["project"] else " (без проекта, останется свободной)"
    lines = [f"<i>Пункт {pos + 1}{NBSP}из{NBSP}{len(queue)}</i>",
             f"<b>{html_escape(entry['name'])}</b> ×{entry['qty']:g}{html_escape(proj_part)}"]
    if entry.get("note"):
        lines.append(f"📝 {html_escape(entry['note'])}")
    if entry.get("user_note"):
        lines.append(f"💬 {html_escape(entry['user_note'])}")
    if entry["matched_id"]:
        lines.append(f"Нашёл в базе: «{html_escape(entry['matched_name'])}» — привяжу её.")
    else:
        lines.append("В базе не нашёл — создам как неуточнённую позицию.")
    kb = {"inline_keyboard": [[
        {"text": "✅ Да", "callback_data": "lay:yes:_"},
        {"text": "⏭ Пропустить", "callback_data": "lay:skip:_"},
        {"text": "⏹ Стоп", "callback_data": "lay:stop:_"},
    ]]}
    send_with_keyboard(chat_id, "\n".join(lines), kb)


def _layout_apply_current(conn, chat_id: int, user_id: int) -> None:
    queue, pos = layout_get(conn, user_id)
    if queue is None or pos >= len(queue):
        return
    entry = queue[pos]
    proj = _resolve_project(conn, entry["project"]) if entry["project"] else None
    extra = " ".join(filter(None, [entry.get("note"), entry.get("user_note")]))
    note_text = (("[неуточнённое] " if entry["uncertain"] else "") + extra).strip()
    if entry["matched_id"]:
        item = get_item(conn, entry["matched_id"])
        if item:
            if note_text:
                item_append_note(conn, item["id"], note_text)
            if proj:
                if entry["qty"] < (item["total_qty"] or 0):
                    item_split_to_project(conn, item["id"], entry["qty"], proj["id"])
                else:
                    item_set_in_project(conn, item["id"], proj["id"])
                send(chat_id, f"🔧 «{item['name']}» → {proj['name']}.")
            else:
                send(chat_id, f"📝 «{item['name']}» — оставил свободной, заметку записал.")
    else:
        new_id = next_item_id(conn)
        now = utc_now()
        status = "in_use" if proj else "stock"
        available = 0 if proj else entry["qty"]
        verified = now if proj else ""
        notes = ("[неуточнённое из описания] " + extra).strip()
        conn.execute(
            "INSERT INTO items (id, name, category, status, total_qty, available_qty, unit, location, notes, description, price_rub, last_verified_at, created_at, updated_at) "
            "VALUES (?, ?, 'unknown', ?, ?, ?, 'pcs', 'unsorted', ?, '', 0, ?, ?, ?)",
            (new_id, entry["name"], status, entry["qty"], available, notes, verified, now, now),
        )
        if proj:
            conn.execute(
                "INSERT INTO item_usage (item_id, project_id, qty, role, since, removable) VALUES (?, ?, ?, '', ?, 1)",
                (new_id, proj["id"], entry["qty"], now[:10]),
            )
        conn.commit()
        entry["created"] = True
        where = f"в проекте {proj['name']}" if proj else "свободной (всплывёт в «новых» для уточнения)"
        send(chat_id, f"➕ Создал «{entry['name']}» {where}.")
    entry["done"] = "yes"
    layout_save_queue(conn, user_id, queue, pos + 1)
    _layout_show_next(conn, chat_id, user_id)


def _handle_inv_text(conn, chat_id: int, user_id: int, sess, text: str) -> bool:
    """Free-form reply during an inventory session. Returns True if consumed."""
    awaiting_id = sess["await_qty_for"]
    current_id = awaiting_id or sess["current_item_id"]
    item = get_item(conn, current_id) if current_id else None
    if not item:
        return False

    # Висит вопрос «Да/Нет»? Текст вместо кнопки = «нет, поясняю» — прошлое
    # понимание сбрасываем и разбираем новое пояснение.
    if inv_get_pending(conn, user_id):
        inv_clear_pending(conn, user_id)

    if awaiting_id and sess["await_kind"] == "cat":
        cat = text.strip().lower()
        item_set_category(conn, awaiting_id, cat)
        inv_clear_await(conn, user_id)
        _edit_export(conn, "category " + awaiting_id)
        send(chat_id, f"🏷 {item['name']} → {cat}.")
        _inv_show_back(conn, chat_id, user_id)
        return True
    if awaiting_id and sess["await_kind"] == "name":
        new_name = text.strip()
        item_set_name(conn, awaiting_id, new_name)
        inv_clear_await(conn, user_id)
        _edit_export(conn, "rename " + awaiting_id)
        send(chat_id, f"✏️ Название → «{new_name}».")
        _inv_show_back(conn, chat_id, user_id)
        return True
    if awaiting_id and sess["await_kind"] == "price":
        p = _parse_qty(text)
        if p is None:
            send(chat_id, "Нужно число, например 320 или 320.5.")
            return True
        item_set_price(conn, awaiting_id, p)
        inv_clear_await(conn, user_id)
        _edit_export(conn, "price " + awaiting_id)
        send(chat_id, f"💰 Цена → {p:g} ₽.")
        _inv_show_back(conn, chat_id, user_id)
        return True
    if awaiting_id and sess["await_kind"] == "source":
        url_m = re.search(r"https?://\S+", text)
        if not url_m:
            send(chat_id, "Пришли ссылку (начинается с http). Или «отмена» под карточкой.")
            return True
        item_set_source(conn, awaiting_id, url_m.group(0))
        inv_clear_await(conn, user_id)
        _edit_export(conn, "source " + awaiting_id)
        send(chat_id, "🔗 Источник обновил.")
        _inv_show_back(conn, chat_id, user_id)
        return True

    qty = _parse_qty(text)
    if qty is not None:
        _inv_apply_action(conn, chat_id, user_id, item, "qty", qty=qty)
        return True
    action = _rule_intent(text)
    if action:
        _inv_apply_action(conn, chat_id, user_id, item, action)
        return True

    try:
        project_names = [p["name"] for p in list_projects(conn)]
        intent = classify_inv_intent(item["name"], _unit_ru(item["unit"]), f"{item['total_qty']:g}",
                                     text, projects=project_names)
    except Exception as exc:
        print(f"inv intent error: {type(exc).__name__}: {exc}", flush=True)
        if awaiting_id:
            send(chat_id, "Нужно число, например 2 или 2.5. Или кнопки «Есть, не считал» / «Отмена».")
            return True
        return False  # пусть ответит обычный чат

    if intent["action"] == "chat":
        return False
    if intent["action"] == "qty" and intent["qty"] is None:
        send(chat_id, "Понял, что речь о количестве, но число не разобрал. Напиши цифрой.")
        return True
    if intent["action"] == "note":
        _inv_apply_action(conn, chat_id, user_id, item, "note", note=intent["note"] or text)
        return True
    if intent["action"] == "in_project":
        _inv_apply_action(conn, chat_id, user_id, item, "in_project", note=intent.get("project") or "")
        return True
    if intent["action"] == "multi":
        if not intent.get("assignments"):
            send(chat_id, "Понял, что разложено по проектам, но не разобрал сколько куда. "
                          "Скажи явно: «одна в DachaNetBox, одна в NetBox, одна свободна».")
            return True
        unit = _unit_ru(item["unit"])
        parts_txt = []
        for a in intent["assignments"]:
            proj = _resolve_project(conn, a["project"])
            if not proj:
                send(chat_id, f"Проект «{a['project']}» не узнал. Назови точнее: FreeNet, FreeNetBox, NetBox, DachaNetBox.")
                return True
            parts_txt.append(f"{a['qty']:g}{NBSP}{unit} → {proj['name']}")
        total_assigned = sum(a["qty"] for a in intent["assignments"])
        free = max(0.0, item["total_qty"] - total_assigned)
        _inv_request_confirm(
            conn, chat_id, user_id,
            {"action": "multi", "item_id": item["id"], "assignments": intent["assignments"]},
            f"Понял так: «{item['name']}» — " + "; ".join(parts_txt) + f"; свободно {free:g}{NBSP}{unit}. Применяю?",
        )
        return True
    if intent["action"] == "split":
        if intent["qty_used"] is None:
            send(chat_id, "Понял, что часть ушла в проект, но не понял сколько. Скажи числом: сколько в проекте и сколько свободно.")
            return True
        proj_name = intent.get("project") or ""
        if not proj_name or not _resolve_project(conn, proj_name):
            send(chat_id, "Понял, что часть ушла в проект, но не понял в какой. "
                          "Скажи целиком, например: «2 ушли во FreeNet, 3 свободны».")
            return True
        unit = _unit_ru(item["unit"])
        free_part = f", свободно {intent['qty']:g}{NBSP}{unit}" if intent["qty"] is not None else ""
        _inv_request_confirm(
            conn, chat_id, user_id,
            {"action": "split", "item_id": item["id"], "qty": intent["qty_used"], "note": proj_name},
            f"Понял так: «{item['name']}» — {intent['qty_used']:g}{NBSP}{unit} в проект "
            f"{proj_name}{free_part}. Применяю?",
        )
        return True
    if intent["action"] == "qty" and intent["note"]:
        # Число с оговорками («2 куска по 3 и 4 см») — не пишем молча, переспрашиваем.
        unit = _unit_ru(item["unit"])
        _inv_request_confirm(
            conn, chat_id, user_id,
            {"action": "qty", "item_id": item["id"], "qty": intent["qty"], "note": intent["note"]},
            f"Понял так: «{item['name']}» — количество {intent['qty']:g}{NBSP}{unit}, "
            f"заметка: «{intent['note']}». Записываю?",
        )
        return True
    _inv_apply_action(conn, chat_id, user_id, item, intent["action"], qty=intent["qty"], note=intent["note"])
    return True


def _inv_request_confirm(conn, chat_id: int, user_id: int, payload: dict, question: str) -> None:
    inv_set_pending(conn, user_id, payload)
    kb = {"inline_keyboard": [[
        {"text": "✅ Да", "callback_data": "inv:cyes:_"},
        {"text": "↩️ Нет, поясню", "callback_data": "inv:cno:_"},
    ]]}
    send_with_keyboard(chat_id, html_escape(question), kb)


def handle_message(conn, message: dict) -> None:
    chat_id = int(message["chat"]["id"])
    chat_type = message.get("chat", {}).get("type", "private")
    user_id = int(message.get("from", {}).get("id", 0))
    print(f"message chat_id={chat_id} chat_type={chat_type} user_id={user_id} text={message.get('text')!r} has_photo={bool(message.get('photo'))}", flush=True)
    # Hard rule: only respond in private chats with explicitly whitelisted users.
    # Don't acknowledge anything outside that — no "access denied", no echoed
    # user_id, no chat activity from groups/channels. Silent drop.
    if chat_type != "private":
        return
    allowed = allowed_user_ids()
    if allowed and user_id not in allowed:
        return

    text = message.get("text") or message.get("caption") or ""

    reply_to = message.get("reply_to_message") or {}
    # Ответ на промпт «название нового проекта».
    if text and not text.startswith("/") and reply_to.get("message_id"):
        prefs = get_preferences(conn)
        if prefs.get("newproj_prompt_msg") == str(reply_to["message_id"]):
            pid, created = project_create(conn, text.strip())
            set_preference(conn, "newproj_prompt_msg", "")
            _edit_export(conn, f"create project {pid}")
            send(chat_id, f"🆕 Проект «{text.strip()}» создан." if created else f"Проект «{text.strip()}» уже есть.")
            _project_card(conn, chat_id, pid)
            return

    # Ответ (reply) на карточку проекта: удалить / переименовать / обновить описание.
    if text and not text.startswith("/") and reply_to.get("message_id"):
        proj_row = project_by_desc_msg(conn, int(reply_to["message_id"]))
        if proj_row:
            low = text.lower().strip()
            if ("проект" in low and any(w in low for w in ("удали", "удалить", "убери", "убрать"))) or low in ("удали", "удалить"):
                cnt = len(project_items(conn, proj_row["id"]))
                kb = {"inline_keyboard": [[
                    {"text": "🗑 Да, удалить", "callback_data": f"prj:delyes:{proj_row['id']}"},
                    {"text": "↩️ Отмена", "callback_data": f"prj:show:{proj_row['id']}"},
                ]]}
                send_with_keyboard(chat_id, f"Удалить проект «{html_escape(proj_row['name'])}»? "
                                            f"Его детали ({cnt}) вернутся на склад свободными.", kb)
                return
            m = re.match(r"^(?:переименуй|назови|переименовать)(?:\s+(?:в|как))?\s+[«\"']?(.+?)[»\"']?$", text.strip(), re.IGNORECASE)
            if m:
                new_name = m.group(1).strip()
                project_rename(conn, proj_row["id"], new_name)
                send(chat_id, f"✏️ Проект «{proj_row['name']}» теперь называется «{new_name}».")
                _project_card(conn, chat_id, proj_row["id"])
                return
            project_set_desc(conn, proj_row["id"], text)
            send(chat_id, f"✏️ Описание «{proj_row['name']}» обновил: {text.strip()}")
            return

    # Ответ (reply) на сообщение-черновик = правка черновика: пересобираем с уточнением.
    if text and not text.startswith("/") and reply_to.get("message_id"):
        row = conn.execute(
            "SELECT id, message_text FROM proposals WHERE draft_message_id = ? AND status = 'pending'",
            (int(reply_to["message_id"]),),
        ).fetchone()
        if row:
            _rebuild_draft(conn, chat_id, user_id, row["id"], row["message_text"], text)
            return

    # Короткое подтверждение при висящем черновике = применить последний черновик.
    # «добавь/применяй» работают час; неоднозначные «да/ок/давай» — только 10 минут,
    # чтобы случайный ответ в разговоре не применил забытый черновик.
    if text and not text.startswith("/"):
        low = text.strip().lower().rstrip("!.")
        strong = low in ("добавь", "добавляй", "применяй", "применить", "применяй давай")
        weak = low in ("да", "ок", "окей", "давай", "го")
        if strong or weak:
            pend = conn.execute(
                "SELECT id, created_at FROM proposals WHERE telegram_user_id = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            max_age = timedelta(minutes=60 if strong else 10)
            fresh = pend and pend["created_at"] >= (datetime.now(timezone.utc) - max_age).replace(microsecond=0).isoformat()
            if fresh:
                repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
                try:
                    result = apply_proposal(conn, pend["id"])
                    send(chat_id, "Готово, внёс в склад.")
                    _enrich_new_items(conn, chat_id, result)
                    export_items(repo_dir)
                    maybe_git_sync(repo_dir, f"apply telegram proposal {pend['id']}")
                except Exception as exc:
                    send(chat_id, f"Не получилось применить черновик #{pend['id']}: {exc}")
                return
            if strong:
                send(chat_id, "Свежего черновика нет. Скажи, что добавить: название и количество, "
                              "либо пришли ссылку на amperkot или фото.")
                return

    # Реакция на висящий черновик БЕЗ формального «ответа»: пока черновик свежий,
    # обычный текст = переделать его (или отменить), не уходя в чат-болтовню.
    if (text and not text.startswith("/") and not message.get("photo")
            and not inv_get(conn, user_id) and layout_get(conn, user_id)[0] is None
            and not _looks_like_stock_question(text)):
        pend = _fresh_pending_draft(conn, user_id)
        if pend:
            low = text.strip().lower().rstrip("!.…")
            NEGATIVE = {"плохо", "нет", "не так", "не то", "неверно", "неправильно",
                        "не пойдёт", "не годится", "мимо", "отмена", "отмени", "не надо"}
            CANCEL = {"отмена", "отмени", "не надо", "выкинь", "удали черновик"}
            if low in CANCEL:
                discard_proposal(conn, pend["id"])
                send(chat_id, "Ок, черновик выкинул.")
                return
            if low in NEGATIVE:
                send(chat_id, "Понял, что мимо. Опиши одним сообщением как надо, "
                              "и я пересоберу черновик. Или «отмена», чтобы выкинуть.")
                return
            # Содержательный текст при свежем черновике = уточнение → пересборка.
            _rebuild_draft(conn, chat_id, user_id, pend["id"], pend["message_text"], text)
            return

    # Активная раскладка устройств: текст = уточнение к текущему пункту.
    lay_queue, lay_pos = layout_get(conn, user_id)
    if lay_queue is not None and text and not text.startswith("/"):
        if lay_pos < len(lay_queue):
            lay_queue[lay_pos]["user_note"] = text.strip()
            layout_save_queue(conn, user_id, lay_queue, lay_pos)
            send(chat_id, "Учёл уточнение — оно ляжет заметкой при подтверждении.")
            _layout_show_next(conn, chat_id, user_id)
            return

    # Inventory mode: free-form replies, numbers and photos apply to the current card.
    sess = inv_get(conn, user_id)
    # Брошенная pick-карточка не должна перехватывать чужие сообщения вечно.
    if sess and sess["mode"] == "pick":
        stale_at = (datetime.now(timezone.utc) - timedelta(minutes=30)).replace(microsecond=0).isoformat()
        if sess["last_action_at"] < stale_at:
            inv_finish(conn, user_id)
            sess = None
    if sess and text and not text.startswith("/") and not message.get("photo"):
        if _handle_inv_text(conn, chat_id, user_id, sess, text):
            return
    if sess and message.get("photo"):
        current_id = sess["await_qty_for"] or sess["current_item_id"]
        item = get_item(conn, current_id) if current_id else None
        if item:
            repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
            best = sorted(message["photo"], key=lambda p: p.get("file_size", 0))[-1]
            caption = (text or "").strip()
            # «замени/замени картинку/замени фото» в подписи = заменить, иначе добавить.
            replace = bool(re.search(r"\bзамен", caption.lower()))
            note = "" if (not caption or replace) else caption
            try:
                rel = download_file(best["file_id"], os.path.join(repo_dir, "photos"))
                if replace:
                    item_set_photo(conn, item["id"], rel)
                    verb = "Заменил фото"
                else:
                    item_add_photo(conn, item["id"], rel)
                    verb = "Добавил фото к"
                if note:
                    item_append_note(conn, item["id"], note)
                try:
                    export_items(repo_dir)
                    maybe_git_sync(repo_dir, "photo for " + item["id"])
                except Exception as exc2:
                    print(f"photo export error: {exc2}", flush=True)
                send(chat_id, f"📷 {verb} «{item['name']}»." + (" Заметку записал." if note else ""))
                # Покажем обновлённую карточку, чтобы было видно результат и кнопки.
                _inv_show_item(conn, chat_id, user_id, get_item(conn, item["id"]))
            except Exception as exc:
                send(chat_id, f"Фото не сохранилось: {exc}")
            return

    if text.startswith("/"):
        handle_command(conn, chat_id, user_id, text)
        return

    repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
    photo_dir = os.path.join(repo_dir, "photos")
    photo_paths = []
    if message.get("photo"):
        best = sorted(message["photo"], key=lambda item: item.get("file_size", 0))[-1]
        photo_paths.append(download_file(best["file_id"], photo_dir))

    if not text and not photo_paths:
        send(chat_id, "Кинь текст или фото с подписью, что это и что с этим сделать.")
        return

    print(f"step: remember user msg", flush=True)
    remember_chat(conn, user_id, "user", text or "[photo]")
    # Фото с вопросом — это распознавание, а не изменение склада.
    if photo_paths and text and ("?" in text or text.lower().startswith(
            ("что", "какая", "какой", "какие", "как", "зачем", "почему", "узнай", "распознай", "определи"))):
        print("step: route=photo-question", flush=True)
        send_chat_action(chat_id, "typing")
        try:
            answer = describe_photo(os.path.join(repo_dir, photo_paths[0]), text)
        except Exception as exc:
            answer = f"Не смог разобрать фото: {exc}"
        remember_chat(conn, user_id, "assistant", answer)
        send(chat_id, answer)
        return
    # Ссылка на amperkot.ru = добавить позицию по карточке магазина (без участия модели).
    amperkot = re.search(r"https?://(?:www\.)?amperkot\.ru/\S+", text or "")
    if amperkot and not photo_paths:
        print("step: route=amperkot-url", flush=True)
        send_chat_action(chat_id, "typing")
        qty_m = re.search(r"(\d+)\s*шт", text or "")
        try:
            _add_from_amperkot(conn, chat_id, user_id, amperkot.group(0), qty=int(qty_m.group(1)) if qty_m else 1)
        except Exception as exc:
            print(f"amperkot scrape error: {exc}", flush=True)
            send(chat_id, f"Не смог снять карточку с amperkot: {exc}. Пришли название, цену и фото — заведу руками.")
        return

    # Маркетплейсы (озон и т.п.): карточку с сервера не достать (защита от ботов),
    # ссылки протухают и не хранятся. Разбираем текст вокруг ссылки, либо просим описание.
    market = re.search(r"https?://(?:www\.)?(ozon\.ru|wildberries\.ru|wb\.ru|aliexpress\.[a-z.]+|avito\.ru)/\S+", text or "")
    if market and not photo_paths:
        rest = (text.replace(market.group(0), "").strip(" \n,—-"))
        if len(rest) >= 12:
            text = rest  # дальше обычный разбор «купил…» уже без ссылки
        else:
            host = market.group(1)
            send(chat_id, f"Карточку с {host} вытащить не могу — пришли название (и цену, если важна) "
                          "текстом или скрин карточки, заведу черновиком.")
            return
    if text and not photo_paths and _looks_like_device_layout(conn, text):
        print("step: route=layout", flush=True)
        _layout_start(conn, chat_id, user_id, text)
        return
    if looks_like_inventory_update(text, bool(photo_paths)):
        print(f"step: route=inventory", flush=True)
        send_chat_action(chat_id, "typing")
        send(chat_id, "Понял, похоже на изменение склада. Сейчас соберу черновик, ничего сам не запишу без подтверждения.")
        try:
            proposal = extract_inventory_proposal(text, [os.path.join(repo_dir, path) for path in photo_paths], _inventory_id_snapshot(conn))
            ops = proposal.get("operations", [])
            if ops and all(o.get("op") == "ask_user" for o in ops):
                # Нечего применять — просто спросим, без черновика и кнопок.
                q = "; ".join(filter(None, (o.get("question") for o in ops))) or "Уточни, что именно добавить."
                remember_chat(conn, user_id, "assistant", q)
                send(chat_id, q)
                return
            # Присланное фото — для ОПОЗНАНИЯ, а не как фото карточки (скриншот заказа
            # не годится для каталога). Нормальное фото товара найдёт обогащение.
            proposal_id = save_proposal(conn, user_id, chat_id, text, [], proposal)
            remember_chat(conn, user_id, "assistant", format_proposal(conn, proposal_id, proposal))
            _send_proposal(conn, chat_id, proposal_id, proposal)
        except Exception as exc:
            reply = (
                "Я принял сообщение, но AI-разбор сейчас не сработал.\n"
                f"Причина: {exc}"
            )
            remember_chat(conn, user_id, "assistant", reply)
            send(chat_id, reply)
        print(f"step: inventory draft sent", flush=True)
    else:
        if text and _looks_like_stock_question(text):
            print("step: route=stock-question", flush=True)
            send_chat_action(chat_id, "typing")
            if _answer_stock_question(conn, chat_id, text):
                remember_chat(conn, user_id, "assistant", "[список позиций по запросу]")
                return
        print(f"step: route=chat, streaming openai", flush=True)
        reply = _chat_with_stream(conn, chat_id, user_id, text)
        remember_chat(conn, user_id, "assistant", reply)
        print(f"step: chat done len={len(reply)}", flush=True)


def set_my_commands() -> None:
    """Register the command menu so Telegram shows hints instead of manual typing."""
    commands = [
        {"command": "list", "description": "📋 Склад по категориям"},
        {"command": "inv", "description": "🔍 Инвентаризация"},
        {"command": "edit", "description": "✏️ Изменить позицию"},
        {"command": "projects", "description": "🛠 Проекты"},
        {"command": "newproject", "description": "🆕 Создать проект"},
        {"command": "start", "description": "ℹ️ Справка"},
    ]
    try:
        telegram("setMyCommands", {"commands": json.dumps(commands, ensure_ascii=False)})
        print("setMyCommands ok", flush=True)
    except Exception as exc:
        print(f"setMyCommands error: {exc}", flush=True)


def main() -> None:
    conn = connect()
    repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
    seed_projects(conn)
    imported = seed_items_from_yaml(conn, repo_dir)
    if imported:
        print(f"seeded inventory from yaml changes={imported}", flush=True)
    set_my_commands()
    offset = 0
    send_errors = 0
    allowed = ",".join(str(item) for item in sorted(allowed_user_ids())) or "none"
    print(f"inventory bot started allowed_user_ids={allowed}", flush=True)
    while True:
        try:
            updates = telegram(
                "getUpdates",
                {"timeout": 10, "offset": offset, "allowed_updates": json.dumps(["message", "callback_query"])},
                timeout=25,
            )
            if updates.get("result"):
                print(f"received updates count={len(updates.get('result', []))} offset={offset}", flush=True)
            for update in updates.get("result", []):
                offset = max(offset, int(update["update_id"]) + 1)
                if "callback_query" in update:
                    try:
                        handle_callback_query(conn, update["callback_query"])
                    except Exception as exc:
                        print(f"callback_query error: {type(exc).__name__}: {exc}", flush=True)
                elif "message" in update:
                    try:
                        handle_message(conn, update["message"])
                    except Exception as exc:
                        print(f"message error: {type(exc).__name__}: {exc}", flush=True)
                        try:
                            send(int(update["message"]["chat"]["id"]), f"Сбой при обработке: {exc}")
                        except Exception:
                            pass
            send_errors = 0
        except Exception as exc:
            send_errors += 1
            print(f"bot error #{send_errors}: {exc}", flush=True)
            time.sleep(min(60, 2 * send_errors))


if __name__ == "__main__":
    main()
