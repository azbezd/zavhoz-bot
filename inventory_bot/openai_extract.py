import base64
import json
import mimetypes
import os
import urllib.error
import urllib.request


SYSTEM_PROMPT = """You maintain a personal electronics inventory for electronics parts, tools, modules and ongoing projects.
Return only JSON matching the schema. Do not invent exact part numbers when unclear.
Use ask_user when a detail is ambiguous or risky. Prefer conservative quantities.
ALL user-facing text (summary, name, notes, question) MUST be in Russian, short and natural —
the user reads it in Telegram. Names like "Резистор 10 кОм (10 шт.)" — Russian word + part marking.
When a photo shows a common hobbyist part, propose the MOST LIKELY specific part by sight
(e.g. VS1838 ИК-приёмник, HC-SR04 дальномер, LM2596 DC-DC) and ask one short confirming
question in Russian including the visible quantity guess: "Похоже на VS1838 (ИК-приёмники), на вид штук 10 — так?"
Better a concrete guess with a question than a vague "unidentified component".
If screenshots contain seller/order/product text, infer purchase status:
- stock: already physically owned
- ordered: bought or waiting for delivery
- wishlist: in cart or considering
- reserved: held for a project
- in_use: already installed in a project
When possible, research or cite the purchase/reference source and summarize practical hardware knowledge:
pinout/manual keywords, voltage, interface, common use, caveats.
If web research is unavailable or uncertain, leave source_url empty and say what needs checking in notes.
Marketplace links (ozon, wildberries, aliexpress, avito) rot quickly and listings are imprecise:
use such a link only to extract the item name, price and photo at add time, but DO NOT store it
in source_url — leave source_url empty unless it is a stable vendor/manufacturer page
(amperkot.ru, chipdip.ru, datasheet sites). Price is optional: use 0 when unknown, never guess.
Supported operations:
- add_item: add a new owned item or wishlist item
- adjust_qty: increase/decrease quantity of an existing item
- mark_used: reserve or use an item in a project
- add_photo: attach a photo to an item
- ask_user: ask a clarification question
"""


JSON_SCHEMA = {
    "name": "inventory_proposal",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "operations"],
        "properties": {
            "summary": {"type": "string"},
            "operations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "op",
                        "item_id",
                        "name",
                        "category",
                        "qty",
                        "unit",
                        "location",
                        "project_id",
                        "notes",
                        "question",
                        "status",
                        "source_title",
                        "source_url",
                        "source_notes",
                        "knowledge_summary",
                        "specs",
                        "confidence",
                    ],
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": ["add_item", "adjust_qty", "mark_used", "add_photo", "ask_user"],
                        },
                        "item_id": {"type": "string"},
                        "name": {"type": "string"},
                        "category": {"type": "string"},
                        "qty": {"type": "number"},
                        "unit": {"type": "string"},
                        "location": {"type": "string"},
                        "project_id": {"type": "string"},
                        "notes": {"type": "string"},
                        "question": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["stock", "ordered", "reserved", "in_use", "consumable", "tool", "wishlist", "lost", "retired"],
                        },
                        "source_title": {"type": "string"},
                        "source_url": {"type": "string"},
                        "source_notes": {"type": "string"},
                        "knowledge_summary": {"type": "string"},
                        "specs": {
                            "type": "string",
                            "description": "Key specs as a JSON object encoded into a string, e.g. {\"voltage\": \"5V\"}. Use \"{}\" when unknown.",
                        },
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                },
            },
        },
    },
    "strict": True,
}


LAYOUT_PROMPT = """Пользователь описывает свои устройства (проекты) и какие детали в них стоят.
Разбери текст на пункты: какая деталь, сколько штук, в каком проекте.

Правила:
- project — ТОЧНО одно из известных имён проектов (передаются в контексте). Если деталь
  не привязана к проекту или проект непонятен — project = "".
- name — нормальное русское название детали с маркировкой, как для склада
  ("Raspberry Pi 3 Model B+", "Модем Fibocom L850-GL", "Дисплей (модель уточнить)").
- search — 2-4 ключевых слова для поиска этой детали в существующей базе (латиница
  для маркировок: "raspberry 3", "fibocom", "корпус алюминиевый zero").
- qty — сколько штук в этом проекте (по умолчанию 1).
- uncertain — true, если деталь описана неточно ("какой-то дисплей", "820 или 850, кажется с литерой L").
- note — краткое уточнение неопределённости или контекст ("модель уточнит позже", "820 или 850 с литерой L").
Если пользователь говорит «таких у меня 2 штуки» про деталь в одном проекте — это qty=2 или
два пункта в разных проектах, смотри по смыслу.
Верни только JSON по схеме."""


LAYOUT_SCHEMA = {
    "name": "device_layout",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["project", "name", "search", "qty", "uncertain", "note"],
                    "properties": {
                        "project": {"type": "string"},
                        "name": {"type": "string"},
                        "search": {"type": "string"},
                        "qty": {"type": "number"},
                        "uncertain": {"type": "boolean"},
                        "note": {"type": "string"},
                    },
                },
            },
        },
    },
    "strict": True,
}


def extract_device_layout(text: str, project_names: list) -> list:
    """Parse a free-form device description into per-item project assignments."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("no OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
    context = "Известные проекты: " + ", ".join(project_names) + "\n\nОписание:\n" + text
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": LAYOUT_PROMPT}]},
            {"role": "user", "content": [{"type": "input_text", "text": context}]},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": LAYOUT_SCHEMA["name"],
                "schema": LAYOUT_SCHEMA["schema"],
                "strict": True,
            }
        },
    }
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    req = urllib.request.Request(
        f"{base_url}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"OpenAI HTTP {exc.code}: {body}") from exc
    for output in raw.get("output", []):
        for part in output.get("content", []):
            if part.get("type") == "output_text":
                return json.loads(part.get("text", "{}")).get("items", [])
    raise RuntimeError("layout: no output_text in response")


ENRICH_PROMPT = """Ты составляешь справочную карточку радиодетали/модуля для каталога.
По названию позиции собери из открытых источников достоверные данные. Если есть веб-поиск —
проверь по datasheet/вики. Не выдумывай: чего не знаешь точно — оставляй пустым.

Верни СТРОГО один JSON:
{"summary": "1-2 предложения что это и для чего, по-русски",
 "description": "развёрнутое описание по-русски (3-6 предложений): назначение, ключевые особенности, типовое применение",
 "specs": {"параметр": "значение", ...},  // например {"Напряжение питания":"3.3–5 В","Интерфейс":"I2C","Разрешение":"128×64"}
 "datasheet_url": "прямая ссылка на datasheet/вики/страницу производителя или пустая строка"}

Параметры в specs — по-русски названия, краткие значения с единицами. 4-10 параметров максимум.
"""


def enrich_item(name: str) -> dict:
    """Обогащение позиции из открытых источников: описание, характеристики, datasheet."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("no OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": ENRICH_PROMPT}]},
            {"role": "user", "content": [{"type": "input_text", "text": f"Позиция: {name}"}]},
        ],
    }
    if os.environ.get("INVENTORY_ENABLE_WEB_SEARCH", "1") == "1":
        payload["tools"] = [{"type": "web_search_preview"}]
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    req = urllib.request.Request(
        f"{base_url}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    parts = []
    for output in raw.get("output", []):
        for part in output.get("content", []):
            if part.get("type") == "output_text":
                parts.append(part.get("text", ""))
    answer = "\n".join(parts).strip()
    start, end = answer.find("{"), answer.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"enrich: no JSON: {answer[:120]!r}")
    data = json.loads(answer[start:end + 1])
    specs = data.get("specs") or {}
    if not isinstance(specs, dict):
        specs = {}
    return {
        "summary": str(data.get("summary") or "").strip(),
        "description": str(data.get("description") or "").strip(),
        "specs": {str(k): str(v) for k, v in specs.items() if v},
        "datasheet_url": str(data.get("datasheet_url") or "").strip(),
    }


def describe_photo(photo_path: str, question: str) -> str:
    """Ответ на вопрос о фото (что это за деталь и т.п.) — без черновиков."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("no OPENAI_API_KEY")
    model = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text":
                "Ты помощник по электронике. Отвечай по-русски, кратко и конкретно. "
                "Если узнаёшь деталь — назови её точно (маркировка, назначение)."}]},
            {"role": "user", "content": [
                {"type": "input_text", "text": question or "Что это за деталь?"},
                {"type": "input_image", "image_url": _data_url(photo_path), "detail": "high"},
            ]},
        ],
    }
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    req = urllib.request.Request(
        f"{base_url}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    parts = []
    for output in raw.get("output", []):
        for part in output.get("content", []):
            if part.get("type") == "output_text":
                parts.append(part.get("text", ""))
    return "\n".join(parts).strip() or "Не разглядел. Пришли фото поближе/при свете."


def scrape_amperkot(url: str) -> dict:
    """Снять имя/цену/фото с карточки amperkot.ru без участия модели."""
    import re, html as _html
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        page = resp.read().decode("utf-8", "replace")
    name = ""
    m = re.search(r"<title>(.*?)</title>", page, re.S)
    if m:
        name = _html.unescape(m.group(1)).strip()
        name = re.sub(r"^(Купить|Заказать)\s+", "", name, flags=re.IGNORECASE).strip()
    price = 0
    for pat in (r'"price"\s*:\s*"?(\d+)', r'itemprop="price"[^>]*content="(\d+)', r'(\d+)\s*(?:&#8381|₽)'):
        pm = re.search(pat, page)
        if pm:
            price = int(pm.group(1)); break
    image = ""
    im = re.search(r'og:image"\s+content="([^"]+)"', page) or re.search(r'"image"\s*:\s*"([^"]+)"', page)
    if im:
        image = im.group(1).replace("\\/", "/")
    return {"name": name, "price": price, "image": image, "url": url}


def _data_url(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    if not mime:
        mime = "image/jpeg"
    with open(path, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def extract_inventory_proposal(text: str, photo_paths: list[str]) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return {
            "summary": "OPENAI_API_KEY is not configured; saved message for manual handling.",
            "operations": [
                {
                    "op": "ask_user",
                    "item_id": "",
                    "name": "",
                    "category": "",
                    "qty": 0,
                    "unit": "",
                    "location": "",
                    "project_id": "",
                    "notes": "",
                    "question": "OpenAI API key is not configured on the server.",
                    "status": "stock",
                    "source_title": "",
                    "source_url": "",
                    "source_notes": "",
                    "knowledge_summary": "",
                    "specs": "{}",
                    "confidence": "low",
                }
            ],
        }

    model = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
    content = [{"type": "input_text", "text": text or "User sent inventory photo(s)."}]
    for path in photo_paths:
        content.append({"type": "input_image", "image_url": _data_url(path), "detail": "high"})

    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": SYSTEM_PROMPT}]},
            {"role": "user", "content": content},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": JSON_SCHEMA["name"],
                "schema": JSON_SCHEMA["schema"],
                "strict": True,
            }
        },
    }
    if os.environ.get("INVENTORY_ENABLE_WEB_SEARCH", "1") == "1":
        payload["tools"] = [{"type": "web_search_preview"}]

    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    req = urllib.request.Request(
        f"{base_url}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"OpenAI HTTP {exc.code}: {body}") from exc

    for output in raw.get("output", []):
        for part in output.get("content", []):
            if part.get("type") == "output_text":
                return json.loads(part.get("text", "{}"))
    if "output_text" in raw:
        return json.loads(raw["output_text"])
    raise RuntimeError("OpenAI response did not contain output_text")
