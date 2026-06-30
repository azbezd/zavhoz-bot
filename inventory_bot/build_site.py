"""Сборка статической веб-витрины склада из базы.

Читает SQLite, пишет самодостаточную папку out_dir: index.html, item/<id>.html,
404.html, assets/style.css и копию photos/. Внешних зависимостей нет (шрифты
системные, скриптов-фреймворков нет). Источник-ссылка ('где купил') хранится
отдельно от фото — фото локальные, ссылка может протухнуть.
"""
import html
import os
import shutil

try:
    from .storage import connect
except ImportError:
    from storage import connect


CATEGORY_LABELS = {
    "computer": "Компьютеры и платформы",
    "microcontroller": "Микроконтроллеры",
    "module": "Модули",
    "sensor": "Сенсоры",
    "emitter": "Излучатели (LED, дисплеи)",
    "semiconductor": "Полупроводники",
    "passive": "Пассивные",
    "connector": "Разъёмы",
    "wire": "Провода",
    "proto": "Платы прототипирования",
    "network": "Сетевое оборудование",
    "power": "Питание",
    "mechanical": "Механика",
    "tool": "Инструменты",
}

E = lambda s: html.escape(str(s or ""))

# Монограмма-фавикон (SVG в data-uri) — без эмодзи, самодостаточно.
FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='7' fill='%230d1014'/%3E"
    "%3Crect x='1.5' y='1.5' width='29' height='29' rx='5.5' fill='none' stroke='%235b9dd9' stroke-width='1.5'/%3E"
    "%3Ctext x='16' y='22' font-family='Segoe UI,Arial,sans-serif' font-size='17' font-weight='700'"
    " fill='%23e8ebf0' text-anchor='middle'%3E%D0%97%3C/text%3E%3C/svg%3E"
)
_CHEVRON = ('<svg class="chev" width="16" height="16" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
            'aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg>')


def _css() -> str:
    return """
:root{
  --bg:#0d1014; --bg2:#11151b; --card:#161b22; --card-h:#1b212b;
  --line:#232a35; --line2:#2e3744; --fg:#e8ebf0; --mut:#97a1b0; --mut2:#6f7a89;
  --acc:#5b9dd9; --acc-soft:rgba(91,157,217,.14); --shadow:rgba(3,6,12,.55);
  --w:1320px; --r:14px; --r-sm:10px;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);
  font:16px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  font-feature-settings:"cv11","ss01";text-rendering:optimizeLegibility;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px;border-radius:4px}
.num{font-variant-numeric:tabular-nums}

header.top{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 88%,transparent);
  backdrop-filter:blur(10px);border-bottom:1px solid var(--line);padding:14px clamp(16px,4vw,40px)}
.top .row{max-width:var(--w);margin:0 auto;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.top .brand{font-size:17px;font-weight:650;letter-spacing:-.01em}
.top .sub{color:var(--mut);font-size:13px}
.back{display:inline-flex;align-items:center;gap:6px;color:var(--mut);font-size:14px;font-weight:500;
  padding:6px 10px;margin:-6px 0;border-radius:var(--r-sm);transition:color .2s,background .2s}
.back:hover{color:var(--fg);background:var(--card)}
.back .chev{transition:transform .2s}.back:hover .chev{transform:translateX(-2px)}

main{max-width:var(--w);margin:0 auto;padding:clamp(16px,3vw,30px) clamp(16px,4vw,40px) 64px}

.search{width:100%;max-width:520px;padding:12px 15px;margin:4px 0 26px;background:var(--bg2);
  border:1px solid var(--line);border-radius:var(--r-sm);color:var(--fg);font-size:15px;transition:border-color .2s}
.search::placeholder{color:var(--mut2)}
.search:focus{outline:none;border-color:var(--acc)}

.cat{display:flex;align-items:baseline;gap:10px;margin:34px 0 14px}
.cat h2{margin:0;font-size:14px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--mut)}
.cat .n{font-size:13px;color:var(--mut2);font-variant-numeric:tabular-nums}
.cat::after{content:"";flex:1;height:1px;background:var(--line);align-self:center}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(178px,1fr));gap:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;
  display:flex;flex-direction:column;transition:transform .18s ease,border-color .18s,box-shadow .18s}
.card:hover{transform:translateY(-3px);border-color:var(--line2);box-shadow:0 10px 26px -12px var(--shadow)}
.card .ph{aspect-ratio:1;background:#0a0d12 center/cover no-repeat;border-bottom:1px solid var(--line)}
.card .ph.empty{display:flex;align-items:center;justify-content:center;color:var(--mut2);font-size:12px}
.card .b{padding:11px 12px 13px;display:flex;flex-direction:column;gap:3px}
.card .nm{font-size:13.5px;line-height:1.4;font-weight:500;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.card .cap{font-size:11.5px;color:var(--mut2)}

/* карточка-страница: две колонки на широком экране */
.item{display:grid;grid-template-columns:minmax(0,440px) minmax(0,1fr);gap:clamp(20px,3vw,44px);align-items:start}
@media (max-width:820px){.item{grid-template-columns:1fr;gap:22px}}
.gallery{position:sticky;top:84px;display:flex;flex-direction:column;gap:12px;min-width:0}
@media (max-width:820px){.gallery{position:static}}
.gallery a{display:block;background:#0a0d12;border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
.gallery img{display:block;width:100%;height:auto}
.gallery .none{padding:48px;text-align:center;color:var(--mut2);background:var(--bg2);
  border:1px dashed var(--line2);border-radius:var(--r)}
.info{min-width:0}
.info h1{margin:0 0 12px;font-size:clamp(22px,3.4vw,30px);line-height:1.18;
  letter-spacing:-.02em;font-weight:680;text-wrap:balance}
.badge{display:inline-block;padding:4px 11px;background:var(--acc-soft);color:var(--acc);
  border-radius:999px;font-size:12.5px;font-weight:600}

.specs{margin:24px 0 0;border-top:1px solid var(--line)}
.specs .pair{display:grid;grid-template-columns:minmax(120px,200px) 1fr;gap:8px 20px;
  padding:11px 2px;border-bottom:1px solid var(--line)}
.specs .k{color:var(--mut);font-size:14px}
.specs .v{font-variant-numeric:tabular-nums}
@media (max-width:520px){.specs .pair{grid-template-columns:1fr;gap:2px;padding:9px 2px}}

.sect{margin:30px 0 10px;font-size:13px;font-weight:600;letter-spacing:.05em;
  text-transform:uppercase;color:var(--mut)}
.prose{max-width:68ch;color:#d6dbe3;line-height:1.65}
.docs{display:flex;flex-direction:column;gap:2px}
.docs a{display:inline-flex;align-items:center;gap:9px;padding:9px 0;color:var(--acc);
  border-bottom:1px solid var(--line);transition:padding-left .2s,color .2s}
.docs a:last-child{border-bottom:0}
.docs a:hover{padding-left:5px}
.docs a::before{content:"";flex:none;width:6px;height:6px;border-right:1.6px solid currentColor;
  border-bottom:1.6px solid currentColor;transform:rotate(-45deg);opacity:.7}

footer{max-width:var(--w);margin:0 auto;padding:30px clamp(16px,4vw,40px);
  color:var(--mut2);font-size:12.5px;border-top:1px solid var(--line)}
.idline{margin-top:30px;color:var(--mut2);font-size:12px;font-variant-numeric:tabular-nums}
.empty-all{padding:80px 20px;text-align:center;color:var(--mut)}
"""


def _head(title, css_href, *, desc="Открытый справочник электронных компонентов: фото, характеристики и официальная документация.") -> str:
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{title}</title><meta name="description" content="{E(desc)}">'
            f'<meta property="og:title" content="{title}"><meta property="og:description" content="{E(desc)}">'
            f'<meta property="og:type" content="website"><meta name="color-scheme" content="dark">'
            f'<link rel="icon" href="{FAVICON}">'
            f'<link rel="stylesheet" href="{css_href}"></head>')


def _first_photo(conn, item_id):
    r = conn.execute("SELECT path FROM item_photos WHERE item_id=? ORDER BY rowid LIMIT 1", (item_id,)).fetchone()
    return r["path"] if r else ""


def _index_html(conn, groups, order, count):
    # Публичный каталог: только справочные данные. Без количеств/наличия.
    sec = []
    for cat in order:
        items = groups[cat]
        sec.append(f'<section class="block" data-cat><div class="cat"><h2>{E(CATEGORY_LABELS.get(cat, cat))}</h2>'
                   f'<span class="n">{len(items)}</span></div><div class="grid">')
        for it in items:
            ph = _first_photo(conn, it["id"])
            ph_html = (f'<div class="ph" style="background-image:url(&quot;{E(ph)}&quot;)"></div>'
                       if ph else '<div class="ph empty">нет фото</div>')
            sec.append(
                f'<a class="card" href="item/{E(it["id"])}.html" data-name="{E(it["name"]).lower()}">'
                f'{ph_html}<div class="b"><div class="nm">{E(it["name"])}</div></div></a>'
            )
        sec.append('</div></section>')
    body = "\n".join(sec)
    return f"""{_head("Завхоз — каталог электроники", "assets/style.css")}<body>
<header class="top"><div class="row"><span class="brand">Завхоз — каталог электроники</span>
<span class="sub num">{count} компонентов · справочник с описаниями и документацией</span></div></header>
<main>
<input class="search" id="q" type="search" placeholder="Поиск по названию" aria-label="Поиск по названию" oninput="flt()">
<div id="list">{body}</div>
<p class="empty-all" id="nores" hidden>Ничего не найдено</p>
</main>
<footer>Открытый справочник компонентов. Наличие и применение хранятся приватно.</footer>
<script>
function flt(){{var v=document.getElementById('q').value.trim().toLowerCase(),shown=0;
document.querySelectorAll('.card').forEach(function(c){{var m=c.dataset.name.indexOf(v)>=0;c.hidden=!m;if(m)shown++;}});
document.querySelectorAll('[data-cat]').forEach(function(s){{
var any=[].some.call(s.querySelectorAll('.card'),function(c){{return !c.hidden;}});s.hidden=!any;}});
document.getElementById('nores').hidden=shown>0;}}
</script>
</body></html>"""


def _item_html(conn, it):
    # Публичная карточка-справочник. БЕЗ количества, наличия, проектов, заметок, цены.
    photos = [r["path"] for r in conn.execute("SELECT path FROM item_photos WHERE item_id=? ORDER BY rowid", (it["id"],))]
    manuals = conn.execute("SELECT url_or_path, title FROM item_manuals WHERE item_id=? ORDER BY rowid", (it["id"],)).fetchall()
    know = conn.execute("SELECT summary, specs_json FROM item_knowledge WHERE item_id=?", (it["id"],)).fetchone()

    if photos:
        gal = "".join(f'<a href="../{E(p)}" target="_blank" rel="noopener">'
                      f'<img src="../{E(p)}" loading="lazy" alt="{E(it["name"])}"></a>' for p in photos)
    else:
        gal = '<div class="none">фото нет</div>'

    pairs = [("Категория", E(CATEGORY_LABELS.get(it["category"], it["category"])))]
    specs = {}
    if know and know["specs_json"]:
        try:
            import json as _json
            specs = _json.loads(know["specs_json"]) or {}
        except Exception:
            specs = {}
    for k, v in specs.items():
        if v:
            pairs.append((E(k), E(v)))
    specs_html = "".join(f'<div class="pair"><div class="k">{k}</div><div class="v">{v}</div></div>' for k, v in pairs)

    extra = []
    summary = know["summary"] if know else ""
    if summary:
        extra.append(f'<div class="sect">Кратко</div><p class="prose">{E(summary)}</p>')
    if it["description"]:
        extra.append(f'<div class="sect">Описание</div><p class="prose">{E(it["description"])}</p>')
    if manuals:
        links = "".join(
            f'<a href="{E(m["url_or_path"])}" target="_blank" rel="noopener">{E(m["title"] or m["url_or_path"])}</a>'
            for m in manuals
        )
        extra.append(f'<div class="sect">Документация</div><div class="docs">{links}</div>')
    extra_html = "\n".join(extra)

    return f"""{_head(E(it["name"]) + " · Завхоз", "../assets/style.css", desc=(summary or it["description"] or it["name"]))}<body>
<header class="top"><div class="row"><a class="back" href="../index.html">{_CHEVRON}Каталог</a></div></header>
<main><article class="item">
<figure class="gallery">{gal}</figure>
<div class="info">
<h1>{E(it["name"])}</h1>
<span class="badge">{E(CATEGORY_LABELS.get(it["category"], it["category"]))}</span>
<div class="specs">{specs_html}</div>
{extra_html}
<div class="idline">id {E(it["id"])}</div>
</div>
</article></main>
</body></html>"""


def _404_html() -> str:
    return f"""{_head("Не найдено · Завхоз", "/zavhoz-web/assets/style.css")}<body>
<header class="top"><div class="row"><a class="back" href="/zavhoz-web/index.html">{_CHEVRON}Каталог</a></div></header>
<main><div class="empty-all"><h1>Страница не найдена</h1>
<p>Такой позиции нет или ссылка устарела.</p>
<p><a class="back" href="/zavhoz-web/index.html">{_CHEVRON}Вернуться в каталог</a></p></div></main>
</body></html>"""


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
        fh.write(_index_html(conn, groups, order, len(items)))
    with open(os.path.join(out_dir, "404.html"), "w", encoding="utf-8") as fh:
        fh.write(_404_html())
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
