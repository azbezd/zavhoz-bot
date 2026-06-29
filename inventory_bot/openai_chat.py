import json
import os
import urllib.error
import urllib.request


SITE_BASE = os.environ.get("ZAVHOZ_SITE_BASE", "https://azbezd.github.io/zavhoz-web")


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
- На вопросы о наличии («сколько резисторов», «какие датчики есть») отвечай СПИСКОМ
  подходящих позиций, по строке на позицию: «Название — N шт», и в конце строки ОБЯЗАТЕЛЬНО
  ссылка на карточку из снапшота (последнее поле строки, голым URL — это наша витрина). Не отвечай голой суммой.
- Когда называешь конкретную позицию склада (в т.ч. на просьбу «дай ссылку») — всегда давай
  ссылку на её карточку из снапшота (последнее поле строки). НИКОГДА не давай ссылок на магазины
  (amperkot, ozon, wildberries, aliexpress) и не выдумывай URL — только карточка из снапшота.
- Полный список склада не пытайся выдать сам, направь пользователя на /list.
  /list рендерится с правильной разметкой и группами; ты вручную лучше не сделаешь.

Внутренние ID позиций (вида hw-2026-001) — это технические, не показывай их пользователю.
Говори названиями товаров.

Правила поведения:
- Отвечай РОВНО на заданный вопрос. Спросили количество — дай количество.
  Не добавляй непрошенное: «что на исходе», советы, предупреждения — только если попросили.
- Не притворяйся, что уже изменил склад, если изменение требует /apply.
- Если пользователь пишет, что купил/нашёл/использовал/потерял детали, скажи, что подготовишь предложение.
- Если пользователь спрашивает проекты, напомни про /projects.
- Если пользователь просто спрашивает совет, отвечай как технический помощник.
- Если не уверен в детали по фото или маркировке, прямо скажи, что нужно уточнить.
- Не раскрывай секреты и токены.
- Не будь канцелярским ботом; говори естественно, но без лишней болтовни.
"""


def inventory_snapshot(conn, limit: int = 500) -> str:
    rows = conn.execute(
        """
        SELECT i.id, i.name, i.category, i.status, i.available_qty, i.total_qty, i.unit
        FROM items i
        WHERE i.status != 'retired'
        ORDER BY i.category, i.name
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        return "Склад пуст."
    # Последнее поле — ссылка на НАШУ карточку в вебе (не на магазин): она не протухает,
    # внутри неё уже есть фото/описание/официальная документация.
    lines = []
    for row in rows:
        card = f"{SITE_BASE}/item/{row['id']}.html"
        lines.append(
            f"{row['name']} | {row['category']} | {row['status']} | "
            f"{row['available_qty']:g}/{row['total_qty']:g} {row['unit']} | {card}"
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


STOCK_QUERY_PROMPT = """Пользователь спрашивает о наличии на своём складе электроники.
Тебе дан пронумерованный список позиций склада. Выбери НОМЕРА строк, которые отвечают
на вопрос. Если спрашивают «сколько резисторов» — выбери ВСЕ позиции-резисторы (каждый
номинал отдельная строка). Если «что есть для ESP32» — всё, что относится к ESP32.

Верни СТРОГО один JSON-объект: {"rows": [числа], "comment": "строка или null"}
- rows — номера подходящих строк (пустой список, если ничего не подходит или вопрос не про наличие).
- comment — одна короткая фраза-дополнение по делу, если есть что добавить (иначе null).
"""


def stock_rows_query(conn, question: str):
    """Подбор строк склада под вопрос «сколько/какие X». Возвращает (rows_list, comment, items)
    где items — выборка list_items_with_sources в том же порядке нумерации."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("no OPENAI_API_KEY")
    try:
        from .storage import list_items_with_sources  # локальный импорт во избежание цикла
    except ImportError:
        from storage import list_items_with_sources
    items = list_items_with_sources(conn)
    numbered = "\n".join(
        f"{i + 1}. {row['name']} | {row['available_qty']:g}/{row['total_qty']:g} {row['unit']}"
        for i, row in enumerate(items)
    )
    model = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": STOCK_QUERY_PROMPT}]},
            {"role": "user", "content": [{"type": "input_text",
                                          "text": f"Склад:\n{numbered}\n\nВопрос: {question}"}]},
        ],
    }
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    req = urllib.request.Request(
        f"{base_url}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    parts = []
    for output in raw.get("output", []):
        for part in output.get("content", []):
            if part.get("type") == "output_text":
                parts.append(part.get("text", ""))
    answer = "\n".join(parts).strip()
    start, end = answer.find("{"), answer.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"stock query: no JSON: {answer[:120]!r}")
    data = json.loads(answer[start:end + 1])
    rows = [int(r) for r in (data.get("rows") or []) if isinstance(r, (int, float))]
    comment = data.get("comment")
    comment = str(comment).strip() if comment else None
    return rows, comment, items


INV_INTENT_PROMPT = """Ты разбираешь ответ пользователя во время инвентаризации склада электроники.
Пользователю показана позиция, он отвечает свободным текстом. Определи намерение.

Верни СТРОГО один JSON-объект без пояснений:
{"action": "...", "qty": число или null, "qty_used": число или null, "note": "строка или null",
 "project": "строка или null", "assignments": [{"project": "...", "qty": число}] или null}

Допустимые action:
- "ok" — подтверждает: позиция на месте, свободна ("да", "есть", "всё ок", "на месте")
- "in_project" — ВСЯ позиция стоит/используется в проекте; положи название проекта в project,
  ТОЧНО как в списке известных проектов ("используется во FreeNet", "стоит в NetBox").
  Если проект не назван или не из списка — project=null.
- "split" — ЧАСТЬ ушла в ОДИН проект, часть осталась свободной ("2 ушли во FreeNet, 3 свободны",
  "один стоит в NetBox, остальные в коробке"). qty_used=сколько в проекте, qty=сколько осталось
  свободно (null если не сказано), project=куда ушло.
- "multi" — позиции разошлись по НЕСКОЛЬКИМ проектам ("одна в DachaNetBox, другая в NetBox",
  "по одному в FreeNet и NetBox, два свободны"). Заполни assignments списком
  [{"project": имя из списка, "qty": сколько туда}], qty=сколько осталось свободно (null если не сказано).
- "consumed" — израсходовано/списано/выброшено ("израсходовал", "списал", "выкинул",
  "использовал все"). Позиция уйдёт из учёта безвозвратно.
- "lost" — позиции просто нет ("нет", "не нашёл", "потерял"). Тоже списывается из учёта.
- "qty" — называет количество/длину; положи число в qty ("осталось 2", "примерно 3 метра", "штуки четыре")
- "uncounted" — позиция есть, но точно посчитать/измерить не может ("есть, но не считал", "не знаю сколько", "размеры не определить")
- "skip" — отложить позицию на потом ("пропусти", "потом", "дальше")
- "stop" — завершить инвентаризацию ("стоп", "хватит", "закончим")
- "pause" — прервать сейчас, продолжить позже ("пауза", "продолжим позже")
- "note" — комментарий/заметка о позиции, положи краткую заметку в note ("лежит в синей коробке", "один разъём погнут")
- "chat" — вопрос или реплика НЕ про учёт этой позиции (например "а для чего этот модуль?"),
  а также сообщение о покупке/добавлении ДРУГОЙ, новой позиции ("купил 10 резисторов") —
  это не ответ по текущей карточке.

Если в тексте и подтверждение, и заметка ("всё ок, лежат в ящике 3") — action="ok", note=заметка.
Если число с уточнением ("2, но один сломан") — action="qty", qty=2, note="один сломан".
ВАЖНО: если ответ описывает размеры/состояние сложнее простого числа ("2 куска по 3 и 4 см") —
action="qty" с qty=общее число И ОБЯЗАТЕЛЬНО note с деталями; бот переспросит подтверждение.
"""


def classify_inv_intent(item_name: str, unit: str, current_qty: str, text: str,
                        projects: list | None = None) -> dict:
    """One cheap non-streaming call. Returns dict with action/qty/note/project.
    Raises on transport/parse errors; caller falls back to rule-based parsing."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("no OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
    projects_line = ", ".join(projects) if projects else "нет"
    context = (
        f"Позиция: {item_name}\nЕдиница учёта: {unit}\nПо базе сейчас: {current_qty}\n"
        f"Известные проекты: {projects_line}\nОтвет пользователя: {text}"
    )
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": INV_INTENT_PROMPT}]},
            {"role": "user", "content": [{"type": "input_text", "text": context}]},
        ],
    }
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    req = urllib.request.Request(
        f"{base_url}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    parts = []
    for output in raw.get("output", []):
        for part in output.get("content", []):
            if part.get("type") == "output_text":
                parts.append(part.get("text", ""))
    answer = "\n".join(parts).strip()
    start, end = answer.find("{"), answer.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"no JSON in intent answer: {answer[:120]!r}")
    data = json.loads(answer[start:end + 1])
    action = str(data.get("action", "")).lower()
    if action not in ("ok", "in_project", "split", "multi", "consumed", "lost", "qty", "uncounted",
                      "skip", "stop", "pause", "note", "chat"):
        raise RuntimeError(f"bad intent action: {action!r}")
    qty = data.get("qty")
    qty = float(qty) if isinstance(qty, (int, float)) else None
    qty_used = data.get("qty_used")
    qty_used = float(qty_used) if isinstance(qty_used, (int, float)) else None
    note = data.get("note")
    note = str(note).strip() if note else None
    project = data.get("project")
    project = str(project).strip() if project else None
    assignments = []
    for a in (data.get("assignments") or []):
        if isinstance(a, dict) and a.get("project") and isinstance(a.get("qty"), (int, float)):
            assignments.append({"project": str(a["project"]).strip(), "qty": float(a["qty"])})
    return {"action": action, "qty": qty, "qty_used": qty_used, "note": note,
            "project": project, "assignments": assignments}


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
