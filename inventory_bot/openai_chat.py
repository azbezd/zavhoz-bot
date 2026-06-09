import json
import os
import urllib.error
import urllib.request


SYSTEM_PROMPT = """Ты Завхоз — умный Telegram-помощник для личного склада электроники.
Говори по-русски, живо, коротко и по делу. Обращайся на "ты".
Твоя задача: помогать вести инвентарь, понимать фото и текст, задавать уточняющие вопросы,
предлагать аккуратные действия и объяснять, что лучше сделать дальше.

ВАЖНО про формат ответов в Telegram:
- НЕ используй markdown: никаких ##, **, ###, ---, обратных кавычек, длинных списков с дефисами.
  Клиент покажет это как сырой текст, выглядит уродливо.
- Пиши обычным текстом. Абзацы разделяй пустой строкой.
- Эмодзи можно умеренно (для акцентов, не каждый абзац).
- Если нужен список — короткие пункты на отдельных строках, без буллетов.
- Полный список склада не пытайся выдать сам, направь пользователя на /list.
  /list рендерится с правильной разметкой и группами; ты вручную лучше не сделаешь.

Внутренние ID позиций (вида hw-2026-001) — это технические, не показывай их пользователю.
Говори названиями товаров.

Правила поведения:
- Не притворяйся, что уже изменил склад, если изменение требует /apply.
- Если пользователь пишет, что купил/нашёл/использовал/потерял детали, скажи, что подготовишь предложение.
- Если пользователь спрашивает проекты, напомни про /projects.
- Если пользователь просто спрашивает совет, отвечай как технический помощник.
- Если не уверен в детали по фото или маркировке, прямо скажи, что нужно уточнить.
- Не раскрывай секреты и токены.
- Не будь канцелярским ботом; говори естественно, но без лишней болтовни.
"""


def inventory_snapshot(conn, limit: int = 30) -> str:
    rows = conn.execute(
        """
        SELECT name, category, status, available_qty, total_qty, unit, location
        FROM items
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        return "Склад пуст."
    lines = []
    for row in rows:
        lines.append(
            f"{row['name']} | {row['category']} | {row['status']} | "
            f"{row['available_qty']:g}/{row['total_qty']:g} {row['unit']} | {row['location']}"
        )
    return "\n".join(lines)


def _build_payload(conn, text: str, recent_messages, preferences: dict, stream: bool) -> tuple:
    model = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
    pref_text = "\n".join(f"{k}: {v}" for k, v in preferences.items()) or "No saved preferences yet."
    context = (
        "Saved user preferences:\n"
        + pref_text
        + "\n\nRecent inventory snapshot:\n"
        + inventory_snapshot(conn)
    )
    messages = [
        {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_PROMPT}]},
        {"role": "system", "content": [{"type": "input_text", "text": context}]},
    ]
    for row in recent_messages:
        if row["role"] == "assistant":
            messages.append({"role": "assistant", "content": [{"type": "output_text", "text": row["text"]}]})
        else:
            messages.append({"role": "user", "content": [{"type": "input_text", "text": row["text"]}]})
    messages.append({"role": "user", "content": [{"type": "input_text", "text": text}]})

    payload = {"model": model, "input": messages}
    if stream:
        payload["stream"] = True
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    return payload, f"{base_url}/responses"


def chat_reply(conn, user_id: int, text: str, recent_messages, preferences: dict) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return "OpenAI API key на сервере не настроен, поэтому пока отвечаю только служебными командами."

    payload, url = _build_payload(conn, text, recent_messages, preferences, stream=False)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"OpenAI HTTP {exc.code}: {body}") from exc

    parts = []
    for output in raw.get("output", []):
        for part in output.get("content", []):
            if part.get("type") == "output_text":
                parts.append(part.get("text", ""))
    if not parts and raw.get("output_text"):
        parts.append(raw["output_text"])
    return "\n".join(part for part in parts if part).strip() or "Я не смог сформулировать ответ. Попробуй ещё раз чуть конкретнее."


def chat_reply_stream(conn, user_id: int, text: str, recent_messages, preferences: dict):
    """Stream OpenAI Responses API. Yields incremental text deltas. The caller
    accumulates them. Raises RuntimeError on HTTP error before the stream opens.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        yield "OpenAI API key на сервере не настроен, поэтому пока отвечаю только служебными командами."
        return

    payload, url = _build_payload(conn, text, recent_messages, preferences, stream=True)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"OpenAI HTTP {exc.code}: {body}") from exc

    # SSE: each event is one or more "data: <json>" lines terminated by blank line.
    data_lines: list[str] = []
    try:
        for raw_line in resp:
            line = raw_line.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                if data_lines:
                    data_str = "\n".join(data_lines)
                    data_lines = []
                    if data_str.strip() == "[DONE]":
                        return
                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type", "")
                    if etype == "response.output_text.delta":
                        delta = event.get("delta", "")
                        if delta:
                            yield delta
                    elif etype == "response.failed":
                        err = event.get("response", {}).get("error", {})
                        raise RuntimeError(f"OpenAI stream failed: {err}")
                continue
            if line.startswith(":"):
                # SSE comment / keep-alive
                continue
            if line.startswith("data: "):
                data_lines.append(line[6:])
            elif line.startswith("data:"):
                data_lines.append(line[5:])
    finally:
        try:
            resp.close()
        except Exception:
            pass
