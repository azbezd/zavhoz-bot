import json
import os
import re
import socket
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone


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
    from .openai_chat import chat_reply, chat_reply_stream, classify_inv_intent
    from .openai_extract import extract_inventory_proposal
    from .storage import (
        apply_proposal,
        connect,
        discard_proposal,
        get_item,
        get_preferences,
        get_project,
        get_proposal,
        inv_clear_await,
        inv_events_for,
        inv_finish,
        inv_get,
        inv_increment_seen,
        inv_log_event,
        inv_mark_lost,
        inv_mark_present,
        inv_mark_qty,
        inv_next_item,
        inv_progress,
        inv_set_await,
        inv_set_current,
        inv_set_pass,
        inv_set_prompt_message,
        inv_set_skipped,
        inv_skipped,
        inv_start,
        item_add_photo,
        item_append_note,
        item_set_category,
        item_set_in_project,
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
    from openai_chat import chat_reply, chat_reply_stream, classify_inv_intent
    from openai_extract import extract_inventory_proposal
    from storage import (
        apply_proposal,
        connect,
        discard_proposal,
        get_item,
        get_preferences,
        get_project,
        get_proposal,
        inv_clear_await,
        inv_events_for,
        inv_finish,
        inv_get,
        inv_increment_seen,
        inv_log_event,
        inv_mark_lost,
        inv_mark_present,
        inv_mark_qty,
        inv_next_item,
        inv_progress,
        inv_set_await,
        inv_set_current,
        inv_set_pass,
        inv_set_prompt_message,
        inv_set_skipped,
        inv_skipped,
        inv_start,
        item_add_photo,
        item_append_note,
        item_set_category,
        item_set_in_project,
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


def send(chat_id: int, text: str, parse_mode: str | None = None, disable_web_page_preview: bool = True) -> None:
    payload = {"chat_id": chat_id, "text": text[:3900]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if disable_web_page_preview:
        payload["disable_web_page_preview"] = "true"
    telegram("sendMessage", payload)


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
    """Send a photo by URL with a caption. Returns the new message_id on success."""
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
    lowered = text.lower()
    markers = [
        "купил",
        "купила",
        "пришло",
        "приехало",
        "добавь",
        "занеси",
        "нашел",
        "нашёл",
        "использовал",
        "поставил",
        "потратил",
        "сломал",
        "потерял",
        "спиши",
        "резистор",
        "esp32",
        "arduino",
        "модуль",
        "провод",
        "датчик",
        "конденсатор",
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


def format_proposal(proposal_id: int, proposal: dict) -> str:
    lines = [f"Черновик #{proposal_id}", proposal.get("summary", "")]
    for idx, op in enumerate(proposal.get("operations", []), start=1):
        lines.append(
            f"{idx}. {op.get('op')} | {op.get('name') or op.get('item_id') or '-'} | "
            f"{op.get('qty')} {op.get('unit') or ''} | {op.get('status') or '-'} | {op.get('location') or '-'}"
        )
        if op.get("knowledge_summary"):
            lines.append(f"   кратко: {op.get('knowledge_summary')}")
        if op.get("source_url"):
            lines.append(f"   источник: {op.get('source_title') or 'source'} — {op.get('source_url')}")
        if op.get("question"):
            lines.append(f"   вопрос: {op.get('question')}")
    lines.append("")
    lines.append(f"Если всё верно: /apply {proposal_id}")
    lines.append(f"Если мимо: /discard {proposal_id}")
    return "\n".join(lines)


def handle_command(conn, chat_id: int, user_id: int, text: str) -> None:
    parts = text.strip().split()
    cmd = parts[0].lower()
    repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())

    if cmd == "/start":
        msg = (
            "Я на связи. Я Завхоз: могу вести склад железок, разбирать фото заказов, "
            "помнить где что лежит и помогать по проектам.\n\n"
            "Пиши обычным языком: «купил 10 резисторов 220 Ом», «это ушло в FreeNetBox», "
            "«что у меня есть для ESP32?». Если я собираюсь менять склад, сначала дам черновик, "
            "а ты подтвердишь через /apply.\n\n"
            "Команды:\n"
            "/list — склад по категориям\n"
            "/inv — инвентаризация (продолжает, если уже идёт)\n"
            "/skipped — вернуться к пропущенным позициям\n"
            "/stop_inv — завершить инвентаризацию со сводкой\n"
            "/projects — проекты\n"
            "/pending — черновики изменений\n"
            "/export — экспорт склада в Git"
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
        # Группировка по category в порядке появления в CATEGORY_LABELS, потом всё остальное
        groups: dict[str, list] = {}
        for row in rows:
            groups.setdefault(row["category"] or "other", []).append(row)
        order = [c for c in CATEGORY_LABELS if c in groups] + [c for c in groups if c not in CATEGORY_LABELS]

        out = ["<b>📋 Склад</b>", f"<i>Всего позиций: {len(rows)}</i>", ""]
        for cat in order:
            items = groups[cat]
            label = CATEGORY_LABELS.get(cat, html_escape(cat))
            out.append(f"<b>{label}</b> · {len(items)}")
            for row in items:
                name = html_escape(row["name"])
                src_url = row["source_url"] if "source_url" in row.keys() else None
                if src_url:
                    name_part = f'<a href="{html_escape(src_url)}">{name}</a>'
                else:
                    name_part = name
                qty = row["available_qty"]
                total = row["total_qty"]
                unit = row["unit"] or "шт"
                if qty == total:
                    qty_part = f"{qty:g} {html_escape(unit)}"
                else:
                    qty_part = f"{qty:g}/{total:g} {html_escape(unit)}"
                out.append(f"• {name_part} — {qty_part}")
            out.append("")
        send_html(chat_id, "\n".join(out).strip())
    elif cmd == "/projects":
        rows = list_projects(conn)
        if not rows:
            send(chat_id, "Проекты пока не заведены.")
            return
        lines = ["Проекты:"]
        for row in rows:
            lines.append(f"{row['id']} | {row['name']} | {row['status']} — {row['description']}")
        send(chat_id, "\n".join(lines))
    elif cmd == "/show" and len(parts) == 2:
        row = get_proposal(conn, int(parts[1]))
        if not row:
            send(chat_id, "Proposal not found.")
            return
        send(chat_id, format_proposal(row["id"], json.loads(row["proposal_json"])))
    elif cmd == "/discard" and len(parts) == 2:
        ok = discard_proposal(conn, int(parts[1]))
        send(chat_id, "Ок, выкинул черновик." if ok else "Такого активного черновика нет.")
    elif cmd == "/apply" and len(parts) == 2:
        result = apply_proposal(conn, int(parts[1]))
        export_items(repo_dir)
        maybe_git_sync(repo_dir, f"apply telegram proposal {int(parts[1])}")
        send(chat_id, "Готово, внёс в склад и экспортировал файлы:\n" + json.dumps(result, ensure_ascii=False, indent=2))
    elif cmd == "/export":
        export_items(repo_dir)
        send(chat_id, "Экспортировал inventory-файлы.")
    elif cmd == "/style" and len(parts) >= 2:
        value = " ".join(parts[1:])
        set_preference(conn, "style", value)
        send(chat_id, f"Запомнил стиль общения: {value}")
    elif cmd in ("/inv", "/inventory"):
        sess = inv_get(conn, user_id)
        if sess:
            done, total_count = inv_progress(conn, user_id)
            kb = {"inline_keyboard": [[
                {"text": f"▶️ Продолжить ({total_count - done} осталось)", "callback_data": "inv:resume:_"},
                {"text": "🔄 Начать заново", "callback_data": "inv:restart:_"},
            ]]}
            send_with_keyboard(chat_id, f"Инвентаризация уже идёт: проверено {done} из {total_count}.", kb)
        else:
            inv_start(conn, user_id, chat_id)
            send(chat_id, "Начинаю инвентаризацию: иду от самых дорогих к самым дешёвым.\n\n"
                          "Можно жать кнопки или отвечать словами: «да», «нет», «осталось 2», "
                          "«есть, но не считал», «потом». Любой другой текст станет заметкой к позиции, "
                          "фото — прикрепится к ней.")
            _inv_advance(conn, chat_id, user_id)
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


def _unit_ru(unit: str) -> str:
    return UNIT_LABELS.get((unit or "pcs").lower(), unit or "шт")


def _inv_keyboard(item_id: str, skipped_count: int = 0, pass_no: int = 1) -> dict:
    rows = [
        [
            {"text": "✅ Свободно", "callback_data": f"inv:ok:{item_id}"},
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


def _inv_show_edit_menu(conn, chat_id: int, item) -> None:
    kb = {"inline_keyboard": [
        [
            {"text": "🔢 Количество", "callback_data": f"inv:qty:{item['id']}"},
            {"text": "🏷 Категория", "callback_data": f"inv:cat:{item['id']}"},
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
    sess = inv_get(conn, user_id)
    pass_no = sess["pass_no"] if sess else 1
    skipped = inv_skipped(conn, user_id)
    done, total_count = inv_progress(conn, user_id)
    name = html_escape(item["name"])
    cat = CATEGORY_LABELS.get(item["category"], html_escape(item["category"] or "—"))
    qty = item["available_qty"]
    total = item["total_qty"]
    unit = html_escape(_unit_ru(item["unit"]))
    price = item["price_rub"] or 0
    status = STATUS_LABELS.get(item["status"], item["status"])
    src_title, src_url = item_first_source(conn, item["id"])
    qty_part = f"{qty:g}{NBSP}{unit}" if qty == total else f"{qty:g}/{total:g}{NBSP}{unit}"
    progress = f"📋 {done + 1}{NBSP}из{NBSP}{total_count}"
    if pass_no >= 2:
        progress += "  ·  ↩️ второй круг"
    lines = [
        f"<i>{progress}</i>",
        f"<b>{name}</b>",
        "",
        f"🏷 {cat}",
        f"🔢 По базе: <b>{qty_part}</b>  ·  {html_escape(status)}",
    ]
    if price:
        lines.append(f"💰 {price:g}{NBSP}₽")
    if src_url:
        lines.append(f'🔗 <a href="{html_escape(src_url)}">{html_escape(src_title or "источник")}</a>')
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
    return True


def _inv_advance(conn, chat_id: int, user_id: int) -> None:
    """Show the next card; switch to the skipped round when the main one ends;
    finish with a summary when nothing is left."""
    if _inv_show_current(conn, chat_id, user_id):
        return
    sess = inv_get(conn, user_id)
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
    ok_count = sum(1 for e in events if e["action"] == "ok")
    uncounted = [e for e in events if e["action"] == "uncounted"]
    qty_changes = [e for e in events if e["action"] == "qty"]
    lost = [e for e in events if e["action"] == "lost"]
    in_projects = [e for e in events if e["action"] == "project"]
    unchecked = max(0, total_count - done)

    lines = [f"<b>Итог инвентаризации</b>  ·  проверено {done} из {total_count}"]
    if ok_count:
        lines.append(f"✅ Подтверждено: {ok_count}")
    if in_projects:
        lines.append("🔧 В проектах:")
        for e in in_projects:
            lines.append(f"  • {html_escape(e['item_name'])}")
    if uncounted:
        lines.append(f"📦 Есть, без пересчёта: {len(uncounted)}")
    if qty_changes:
        lines.append("✏️ Изменено количество:")
        for e in qty_changes:
            old = f"{e['old_total']:g}" if e["old_total"] is not None else "?"
            new = f"{e['new_total']:g}" if e["new_total"] is not None else "?"
            lines.append(f"  • {html_escape(e['item_name'])}: {old} → {new}")
    if lost:
        lines.append("❌ Потеряно:")
        for e in lost:
            lines.append(f"  • {html_escape(e['item_name'])}")
    if unchecked:
        lines.append(f"⏭ Не проверено: {unchecked} — всплывут при следующем /inv")
    inv_finish(conn, user_id)

    synced = ""
    if qty_changes or lost or in_projects:
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

    if not data.startswith("inv:"):
        answer_callback(cb_id)
        return

    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    item_id = parts[2] if len(parts) > 2 else ""

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
        _inv_advance(conn, chat_id, user_id)
        return
    if action == "pcancel":
        answer_callback(cb_id, "Отменил")
        _inv_advance(conn, chat_id, user_id)
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
        send(chat_id, f"🏷 {item['name']} → {label}. Карточка обновится, жду ответа по наличию.")
        _inv_advance(conn, chat_id, user_id)
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
    if action == "ok" and item:
        inv_mark_present(conn, item_id)
        inv_increment_seen(conn, user_id)
        inv_log_event(conn, user_id, item_id, item["name"], "ok")
        answer_callback(cb_id, "Отмечено: всё на месте")
    elif action == "lost" and item:
        inv_log_event(conn, user_id, item_id, item["name"], "lost", old_total=item["total_qty"])
        inv_mark_lost(conn, item_id)
        inv_increment_seen(conn, user_id)
        answer_callback(cb_id, "Отмечено: потеряно")
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
                      qty=None, note: str | None = None) -> None:
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
        inv_log_event(conn, user_id, item_id, name, "lost", old_total=item["total_qty"])
        inv_mark_lost(conn, item_id)
        inv_clear_await(conn, user_id)
        inv_increment_seen(conn, user_id)
        send(chat_id, f"❌ {name} — отметил потерянной.{noted}")
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
        send(chat_id, f"📝 Записал заметку к «{name}». Карточка ждёт ответа.")
    else:
        send(chat_id, "Не понял. Нажми кнопку под карточкой или напиши число/«есть»/«нет».")


def _handle_inv_text(conn, chat_id: int, user_id: int, sess, text: str) -> bool:
    """Free-form reply during an inventory session. Returns True if consumed."""
    awaiting_id = sess["await_qty_for"]
    current_id = awaiting_id or sess["current_item_id"]
    item = get_item(conn, current_id) if current_id else None
    if not item:
        return False

    if awaiting_id and sess["await_kind"] == "cat":
        cat = text.strip().lower()
        item_set_category(conn, awaiting_id, cat)
        inv_clear_await(conn, user_id)
        send(chat_id, f"🏷 {item['name']} → {cat}. Карточка обновится, жду ответа по наличию.")
        _inv_advance(conn, chat_id, user_id)
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
    _inv_apply_action(conn, chat_id, user_id, item, intent["action"], qty=intent["qty"], note=intent["note"])
    return True


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

    # Inventory mode: free-form replies, numbers and photos apply to the current card.
    sess = inv_get(conn, user_id)
    if sess and text and not text.startswith("/") and not message.get("photo"):
        if _handle_inv_text(conn, chat_id, user_id, sess, text):
            return
    if sess and message.get("photo"):
        current_id = sess["await_qty_for"] or sess["current_item_id"]
        item = get_item(conn, current_id) if current_id else None
        if item:
            repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
            best = sorted(message["photo"], key=lambda p: p.get("file_size", 0))[-1]
            try:
                rel = download_file(best["file_id"], os.path.join(repo_dir, "photos"))
                item_add_photo(conn, item["id"], rel)
                if text:
                    item_append_note(conn, item["id"], text)
                send(chat_id, f"📷 Прикрепил фото к «{item['name']}»."
                              + (" Заметку записал." if text else "")
                              + " Карточка ждёт ответа.")
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
    if looks_like_inventory_update(text, bool(photo_paths)):
        print(f"step: route=inventory", flush=True)
        send_chat_action(chat_id, "typing")
        send(chat_id, "Понял, похоже на изменение склада. Сейчас соберу черновик, ничего сам не запишу без подтверждения.")
        try:
            proposal = extract_inventory_proposal(text, [os.path.join(repo_dir, path) for path in photo_paths])
            proposal_id = save_proposal(conn, user_id, chat_id, text, photo_paths, proposal)
            reply = format_proposal(proposal_id, proposal)
        except Exception as exc:
            reply = (
                "Я принял сообщение, но AI-разбор сейчас не сработал.\n"
                f"Причина: {exc}\n\n"
                "Фото/текст можно разобрать позже, когда настроим AI-доступ с поддерживаемого региона."
            )
        print(f"step: inventory reply ready len={len(reply)}", flush=True)
        remember_chat(conn, user_id, "assistant", reply)
        send(chat_id, reply)
        print(f"step: inventory sent", flush=True)
    else:
        print(f"step: route=chat, streaming openai", flush=True)
        reply = _chat_with_stream(conn, chat_id, user_id, text)
        remember_chat(conn, user_id, "assistant", reply)
        print(f"step: chat done len={len(reply)}", flush=True)


def set_my_commands() -> None:
    """Register the command menu so Telegram shows hints instead of manual typing."""
    commands = [
        {"command": "list", "description": "📋 Склад по категориям"},
        {"command": "inv", "description": "🔍 Инвентаризация (старт/продолжить)"},
        {"command": "skipped", "description": "↩️ К пропущенным позициям"},
        {"command": "stop_inv", "description": "⏹ Завершить инвентаризацию"},
        {"command": "projects", "description": "🛠 Проекты"},
        {"command": "pending", "description": "📝 Черновики изменений"},
        {"command": "export", "description": "💾 Экспорт склада в Git"},
        {"command": "start", "description": "ℹ️ Что умеет бот"},
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
