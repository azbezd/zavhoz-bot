import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from .export_inventory import export_items, maybe_git_sync
    from .openai_chat import chat_reply
    from .openai_extract import extract_inventory_proposal
    from .storage import (
        apply_proposal,
        connect,
        discard_proposal,
        get_preferences,
        get_proposal,
        list_items,
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
    from openai_chat import chat_reply
    from openai_extract import extract_inventory_proposal
    from storage import (
        apply_proposal,
        connect,
        discard_proposal,
        get_preferences,
        get_proposal,
        list_items,
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
        return json.loads(resp.read().decode("utf-8"))


def send(chat_id: int, text: str) -> None:
    telegram("sendMessage", {"chat_id": chat_id, "text": text[:3900]})


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
    repo_rel = os.path.join("inventory", "photos", local_name)
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
        rows = list_items(conn)
        if not rows:
            send(chat_id, "Склад пока пустой.")
            return
        lines = ["Склад:"]
        for row in rows:
            lines.append(
                f"{row['id']} | {row['name']} | {row['status']} | "
                f"{row['available_qty']:g}/{row['total_qty']:g} {row['unit']} | {row['location']}"
            )
        send(chat_id, "\n".join(lines))
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


def handle_message(conn, message: dict) -> None:
    chat_id = int(message["chat"]["id"])
    user_id = int(message.get("from", {}).get("id", 0))
    print(f"message chat_id={chat_id} user_id={user_id} text={message.get('text')!r} has_photo={bool(message.get('photo'))}", flush=True)
    allowed = allowed_user_ids()
    if allowed and user_id not in allowed:
        send(chat_id, f"Access denied. Your Telegram user ID: {user_id}")
        return

    text = message.get("text") or message.get("caption") or ""
    if text.startswith("/"):
        handle_command(conn, chat_id, user_id, text)
        return

    repo_dir = os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
    photo_dir = os.path.join(repo_dir, "inventory", "photos")
    photo_paths = []
    if message.get("photo"):
        best = sorted(message["photo"], key=lambda item: item.get("file_size", 0))[-1]
        photo_paths.append(download_file(best["file_id"], photo_dir))

    if not text and not photo_paths:
        send(chat_id, "Кинь текст или фото с подписью, что это и что с этим сделать.")
        return

    remember_chat(conn, user_id, "user", text or "[photo]")
    if looks_like_inventory_update(text, bool(photo_paths)):
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
        remember_chat(conn, user_id, "assistant", reply)
        send(chat_id, reply)
    else:
        try:
            reply = chat_reply(conn, user_id, text, recent_chat(conn, user_id), get_preferences(conn))
        except Exception as exc:
            reply = (
                "Я вижу сообщение, но AI-ответ сейчас не сработал.\n"
                f"Причина: {exc}\n\n"
                "Складовые команды без AI работают: /list, /projects, /pending."
            )
        remember_chat(conn, user_id, "assistant", reply)
        send(chat_id, reply)


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
