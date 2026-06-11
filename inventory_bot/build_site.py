"""Сборка статической веб-витрины склада из базы.

Читает SQLite, пишет самодостаточную папку out_dir: index.html, item/<id>.html,
assets/style.css и копию photos/. Внешних зависимостей нет. Источник-ссылка
('где купил') хранится отдельно от фото — фото локальные, ссылка может протухнуть.
"""
import html
import os
import shutil

try:
    from .storage import connect
except ImportError:
    from storage import connect


CATEGORY_LABELS = {
    "computer": "🖥 Компьютеры и платформы",
    "microcontroller": "🧠 Микроконтроллеры",
    "module": "📦 Модули",
    "sensor": "📡 Сенсоры",
    "emitter": "💡 Излучатели (LED, дисплеи)",
    "semiconductor": "⚡️ Полупроводники",
    "passive": "🔘 Пассивные",
    "connector": "🔌 Разъёмы",
    "wire": "🪢 Провода",
    "proto": "🟫 Платы прототипирования",
    "network": "🌐 Сетевое оборудование",
    "power": "🔋 Питание",
    "mechanical": "🔩 Механика",
    "tool": "🛠 Инструменты",
}
STATUS_LABELS = {
    "stock": "в наличии", "consumable": "расходник", "tool": "инструмент",
    "in_use": "в проекте", "ordered": "ожидаю", "reserved": "зарезервировано",
    "wishlist": "хочу купить", "lost": "потеряно", "retired": "списано",
}

E = lambda s: html.escape(str(s or ""))


def _css() -> str:
    return """
:root{--bg:#0e1116;--card:#171c24;--line:#252c38;--fg:#e6e9ef;--mut:#9aa4b2;--acc:#4ea1ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;background:rgba(14,17,22,.92);backdrop-filter:blur(6px);border-bottom:1px solid var(--line);padding:14px 18px;z-index:5}
header h1{margin:0;font-size:18px}header .sub{color:var(--mut);font-size:13px}
.wrap{max-width:1000px;margin:0 auto;padding:18px}
.search{width:100%;padding:11px 14px;margin:0 0 16px;background:var(--card);border:1px solid var(--line);border-radius:10px;color:var(--fg);font-size:15px}
.cat{margin:22px 0 8px;font-size:15px;color:var(--mut);letter-spacing:.02em}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;display:flex;flex-direction:column}
.card .ph{aspect-ratio:1;background:#0b0e13 center/cover no-repeat;border-bottom:1px solid var(--line)}
.card .b{padding:9px 10px}
.card .nm{font-size:13px;line-height:1.35;max-height:3.6em;overflow:hidden}
.card .q{color:var(--mut);font-size:12px;margin-top:4px}
.item{max-width:780px;margin:0 auto}
.gal{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0 16px}
.gal img{max-width:100%;width:320px;border-radius:12px;border:1px solid var(--line)}
.kv{display:grid;grid-template-columns:140px 1fr;gap:6px 14px;margin:12px 0}
.kv .k{color:var(--mut)}
.badge{display:inline-block;padding:2px 9px;border:1px solid var(--line);border-radius:999px;font-size:13px;color:var(--mut)}
.back{display:inline-block;margin:0 0 6px;color:var(--mut)}
.sect{margin:18px 0 6px;font-size:14px;color:var(--mut)}
footer{color:var(--mut);font-size:12px;text-align:center;padding:24px}
"""


def _first_photo(conn, item_id):
    r = conn.execute("SELECT path FROM item_photos WHERE item_id=? ORDER BY rowid LIMIT 1", (item_id,)).fetchone()
    return r["path"] if r else ""


def _index_html(conn, items, groups, order, count):
    # Публичный каталог: только справочные данные. Без количеств/наличия.
    cards = []
    for cat in order:
        cards.append(f'<div class="cat" data-cat>{E(CATEGORY_LABELS.get(cat, cat))} · {len(groups[cat])}</div>')
        cards.append('<div class="grid">')
        for it in groups[cat]:
            ph = _first_photo(conn, it["id"])
            phstyle = f'style="background-image:url({E(ph)})"'
            cards.append(
                f'<a class="card" href="item/{E(it["id"])}.html" data-name="{E(it["name"]).lower()}">'
                f'<div class="ph" {phstyle}></div>'
                f'<div class="b"><div class="nm">{E(it["name"])}</div></div></a>'
            )
        cards.append('</div>')
    body = "\n".join(cards)
    return f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Завхоз · каталог электроники</title><link rel=stylesheet href="assets/style.css"></head><body>
<header><h1>🧰 Завхоз — каталог электроники</h1><div class=sub>{count} компонентов · справочник с описаниями и документацией</div></header>
<div class=wrap>
<input class=search id=q placeholder="Поиск по названию…" oninput="flt()">
{body}
<footer>Открытый справочник компонентов. Наличие и применение хранятся приватно.</footer>
</div>
<script>
function flt(){{var v=document.getElementById('q').value.toLowerCase();
document.querySelectorAll('.card').forEach(function(c){{c.style.display=c.dataset.name.indexOf(v)>=0?'':'none';}});
document.querySelectorAll('[data-cat]').forEach(function(h){{var g=h.nextElementSibling;var any=[...g.querySelectorAll('.card')].some(function(c){{return c.style.display!=='none';}});h.style.display=any?'':'none';g.style.display=any?'':'none';}});}}
</script>
</body></html>"""


def _item_html(conn, it):
    # Публичная карточка-справочник. БЕЗ количества, наличия, проектов, заметок, цены.
    photos = [r["path"] for r in conn.execute("SELECT path FROM item_photos WHERE item_id=? ORDER BY rowid", (it["id"],))]
    manuals = conn.execute("SELECT url_or_path, title FROM item_manuals WHERE item_id=? ORDER BY rowid", (it["id"],)).fetchall()
    know = conn.execute("SELECT summary, specs_json FROM item_knowledge WHERE item_id=?", (it["id"],)).fetchone()

    gal = "".join(f'<a href="../{E(p)}" target=_blank><img src="../{E(p)}" loading=lazy></a>' for p in photos) or "<i>фото нет</i>"
    kv = [("Категория", E(CATEGORY_LABELS.get(it["category"], it["category"])))]
    # Характеристики из knowledge.specs_json (словарь параметр→значение).
    specs = {}
    if know and know["specs_json"]:
        try:
            import json as _json
            specs = _json.loads(know["specs_json"]) or {}
        except Exception:
            specs = {}
    for k, v in specs.items():
        if v:
            kv.append((E(k), E(v)))
    kv_html = "".join(f'<div class="k">{k}</div><div>{v}</div>' for k, v in kv)

    extra = []
    if it["description"]:
        extra.append(f'<div class=sect>Описание</div><div>{E(it["description"])}</div>')
    if know and know["summary"]:
        extra.append(f'<div class=sect>Кратко</div><div>{E(know["summary"])}</div>')
    if manuals:
        links = "<br>".join(
            f'<a href="{E(m["url_or_path"])}" target=_blank rel=noopener>{E(m["title"] or m["url_or_path"])}</a>'
            for m in manuals
        )
        extra.append(f'<div class=sect>Документация</div><div>{links}</div>')
    extra_html = "\n".join(extra)

    return f"""<!doctype html><html lang=ru><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{E(it["name"])} · Завхоз</title><link rel=stylesheet href="../assets/style.css"></head><body>
<header><h1><a class=back href="../index.html">← каталог</a></h1></header>
<div class=wrap><div class=item>
<h2>{E(it["name"])}</h2>
<div class=gal>{gal}</div>
<div class=kv>{kv_html}</div>
{extra_html}
<footer>id {E(it["id"])}</footer>
</div></div></body></html>"""


def build(out_dir: str, repo_dir: str | None = None) -> int:
    """repo_dir — где лежат photos/ (для копирования). Возвращает число позиций."""
    conn = connect()
    items = conn.execute(
        "SELECT * FROM items WHERE status != 'retired' ORDER BY category, name"
    ).fetchall()
    groups: dict = {}
    for it in items:
        groups.setdefault(it["category"] or "other", []).append(it)
    order = [c for c in CATEGORY_LABELS if c in groups] + [c for c in groups if c not in CATEGORY_LABELS]

    os.makedirs(os.path.join(out_dir, "assets"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "item"), exist_ok=True)
    with open(os.path.join(out_dir, "assets", "style.css"), "w", encoding="utf-8") as fh:
        fh.write(_css())
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(_index_html(conn, items, groups, order, len(items)))
    for it in items:
        with open(os.path.join(out_dir, "item", f'{it["id"]}.html'), "w", encoding="utf-8") as fh:
            fh.write(_item_html(conn, it))

    # Копируем фото внутрь сайта, чтобы он был самодостаточным.
    repo_dir = repo_dir or os.environ.get("INVENTORY_REPO_DIR", os.getcwd())
    src_photos = os.path.join(repo_dir, "photos")
    dst_photos = os.path.join(out_dir, "photos")
    if os.path.isdir(src_photos):
        os.makedirs(dst_photos, exist_ok=True)
        for f in os.listdir(src_photos):
            s = os.path.join(src_photos, f)
            if os.path.isfile(s):
                shutil.copy2(s, os.path.join(dst_photos, f))
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    return len(items)


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "site"
    n = build(out)
    print(f"site built: {n} items -> {out}")
