import json
import os
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
    from .openai_chat import chat_reply, chat_reply_stream
    from .openai_extract import extract_inventory_proposal
    from .storage import (
        apply_proposal,
        connect,
        discard_proposal,
        get_preferences,
        get_proposal,
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
    from openai_chat import chat_reply, chat_reply_stream
    from openai_extract import extract_inventory_proposal
    from storage import (
        apply_proposal,
        connect,
        discard_proposal,
        get_preferences,
        get_proposal,
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
            "Команды: /list, /projects, /pending, /show <id>, /apply <id>, /discard <id>, /export."
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
        maybe_git_sync(repo_dir, int(parts[1]))
        send(chat_id, "Готово, внёс в склад и экспортировал файлы:\n" + json.dumps(result, ensure_ascii=False, indent=2))
    elif cmd == "/export":
        export_items(repo_dir)
        send(chat_id, "Экспортировал inventory-файлы.")
    elif cmd == "/style" and len(parts) >= 2:
        value = " ".join(parts[1:])
        set_preference(conn, "style", value)
        send(chat_id, f"Запомнил стиль общения: {value}")
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


def main() -> None:
    conn = connect()
    repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
    seed_projects(conn)
    imported = seed_items_from_yaml(conn, repo_dir)
    if imported:
        print(f"seeded inventory from yaml changes={imported}", flush=True)
    offset = 0
    send_errors = 0
    allowed = ",".join(str(item) for item in sorted(allowed_user_ids())) or "none"
    print(f"inventory bot started allowed_user_ids={allowed}", flush=True)
    while True:
        try:
            updates = telegram("getUpdates", {"timeout": 10, "offset": offset}, timeout=25)
            if updates.get("result"):
                print(f"received updates count={len(updates.get('result', []))} offset={offset}", flush=True)
            for update in updates.get("result", []):
                offset = max(offset, int(update["update_id"]) + 1)
                if "message" in update:
                    handle_message(conn, update["message"])
            send_errors = 0
        except Exception as exc:
            send_errors += 1
            print(f"bot error #{send_errors}: {exc}", flush=True)
            time.sleep(min(60, 2 * send_errors))


if __name__ == "__main__":
    main()
