import base64
import json
import mimetypes
import os
import urllib.error
import urllib.request


SYSTEM_PROMPT = """You maintain a personal electronics inventory for electronics parts, tools, modules and ongoing projects.
Return only JSON matching the schema. Do not invent exact part numbers when unclear.
Use ask_user when a detail is ambiguous or risky. Prefer conservative quantities.
If screenshots contain seller/order/product text, infer purchase status:
- stock: already physically owned
- ordered: bought or waiting for delivery
- wishlist: in cart or considering
- reserved: held for a project
- in_use: already installed in a project
When possible, research or cite the purchase/reference source and summarize practical hardware knowledge:
pinout/manual keywords, voltage, interface, common use, caveats.
If web research is unavailable or uncertain, leave source_url empty and say what needs checking in notes.
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
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                },
            },
        },
    },
    "strict": True,
}


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
                    "specs": {},
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
