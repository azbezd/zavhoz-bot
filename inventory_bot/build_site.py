"""Сборка статической веб-витрины склада из базы.

Читает SQLite, пишет самодостаточную папку out_dir: index.html, item/<id>.html,
404.html, assets/style.css и копию photos/. Внешних зависимостей нет (шрифты
системные, скриптов-фреймворков нет). Источник-ссылка ('где купил') хранится
отдельно от фото — фото локальные, ссылка может протухнуть.
"""
import html
import os
import shutil
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from .safety import item_id_valid, web_url

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
    "storage": "Хранение",
    "other": "Прочее",
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


def _head(title, prefix="", desc="Фото, назначение, характеристики и документация электронных компонентов."):
    return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'none'; object-src 'none'">
<meta name="referrer" content="no-referrer"><meta name="color-scheme" content="dark">
<title>{E(title)}</title><meta name="description" content="{E(desc)}">
<link rel="icon" href="{FAVICON}"><link rel="stylesheet" href="{prefix}assets/style.css">
<script src="{prefix}assets/catalog.js" defer></script></head><body>
<a class="skip" href="#main">К содержимому</a>'''


def _knowledge(conn, item_id):
    row = conn.execute("SELECT * FROM item_knowledge WHERE item_id=?", (item_id,)).fetchone()
    try:
        specs = json.loads(row["specs_json"]) if row else {}
    except (TypeError, ValueError):
        specs = {}
    return (row["summary"] if row else ""), specs if isinstance(specs, dict) else {}


def _photos(conn, item_id):
    return [r[0] for r in conn.execute("SELECT p.path FROM item_photos p JOIN approved a ON a.path=p.path WHERE p.item_id=? ORDER BY p.rowid", (item_id,))]


def _index_html(conn, groups, order, count):
    sections=[]
    for cat in order:
        label=CATEGORY_LABELS.get(cat,cat)
        cards=[]
        for it in groups[cat]:
            summary,specs=_knowledge(conn,it['id'])
            photos=_photos(conn,it['id'])
            picture=(f'<img class="ph" src="{E(photos[0])}" alt="" loading="lazy" width="280" height="280">' if photos else '<div class="ph empty">Фото уточняется</div>')
            search=' '.join([it['name'],it['id'],label,summary,it['description'],json.dumps(specs,ensure_ascii=False)])
            cards.append(f'<a class="card" aria-label="{E(it["name"])}" href="item/{E(it["id"])}.html" data-search="{E(search)}">{picture}<div class="b"><div class="nm">{E(it["name"])}</div><p class="summary">{E(summary or it["description"])}</p></div></a>')
        sections.append(f'<section class="block" data-cat="{E(cat)}"><div class="cat"><h2>{E(label)}</h2><span class="n">{len(cards)}</span></div><div class="grid">{"".join(cards)}</div></section>')
    options=''.join(f'<option value="{E(c)}">{E(CATEGORY_LABELS.get(c,c))}</option>' for c in order)
    return f'''{_head("Завхоз · Справочник электроники")}
<header class="top"><div class="row"><span class="brand">Завхоз</span><span class="sub">Справочник электроники</span></div></header>
<main id="main"><h1>Найди нужную деталь</h1><p class="intro">Фото, назначение и документация — всё в карточке компонента.</p>
<div class="filters"><label>Название или назначение<input id="q" type="search" placeholder="Например, PCA9685 или сервопривод" autocomplete="off"></label>
<label>Категория<select id="category"><option value="">Все категории</option>{options}</select></label><button id="clear" type="button">Сбросить</button></div>
<p id="results" class="results" role="status" aria-live="polite">Компонентов: {count}</p>
<div id="list">{''.join(sections)}</div><div id="nores" class="empty-all" hidden><h2>Ничего не найдено</h2><p>Сократи запрос или выбери другую категорию.</p></div></main>
<footer>Открытый справочник. Количество, покупки и применение в проектах хранятся приватно.</footer></body></html>'''


def _item_html(conn,it):
    summary,specs=_knowledge(conn,it['id'])
    photos=_photos(conn,it['id'])
    gal=''.join(f'<a href="../{E(p)}" target="_blank" rel="noopener"><img src="../{E(p)}" alt="{E(it["name"])}" loading="lazy"></a>' for p in photos) or '<div class="none">Фото уточняется</div>'
    docs=conn.execute("SELECT url_or_path,title FROM item_manuals WHERE item_id=? ORDER BY rowid",(it['id'],)).fetchall()
    links=''.join(f'<a href="{E(d["url_or_path"])}" target="_blank" rel="noopener noreferrer">{E(d["title"] or "Документация")}</a>' for d in docs if web_url(d['url_or_path']))
    pairs=''.join(f'<div class="pair"><div class="k">{E(k)}</div><div class="v">{E(v)}</div></div>' for k,v in specs.items() if v is not None and v!='')
    description=it['description'] if it['description']!=summary else ''
    return f'''{_head(it['name']+' · Завхоз','../',summary or it['name'])}
<header class="top"><div class="row"><a class="back" id="back" href="../index.html">{_CHEVRON}Все компоненты</a></div></header>
<main id="main"><article class="item"><div class="lead"><span class="badge">{E(CATEGORY_LABELS.get(it['category'],it['category']))}</span><h1>{E(it['name'])}</h1>
<p class="prose">{E(summary)}</p></div><figure class="gallery">{gal}</figure><div class="info">{('<h2 class="sect">Документация</h2><div class="docs">'+links+'</div>') if links else ''}
{('<h2 class="sect">Характеристики</h2><div class="specs">'+pairs+'</div>') if pairs else ''}
{('<h2 class="sect">Подробнее</h2><p class="prose">'+E(description)+'</p>') if description else ''}
<div class="idline">{E(it['id'])}</div></div></article></main></body></html>'''


def build(out_dir,repo_dir=None):
    """Build from a consistent snapshot; only approved referenced photo bytes leave it."""
    root=Path(repo_dir or os.environ.get('INVENTORY_REPO_DIR',os.getcwd())).resolve()
    out=Path(out_dir).absolute()
    if out.is_symlink() or out.resolve()==root or out.resolve() in root.parents:
        raise ValueError('Output must be separate from private inventory')
    source=connect()
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    try:
        source.backup(conn)
    finally:
        source.close()
    try:
        conn.execute('CREATE TEMP TABLE approved(path TEXT PRIMARY KEY)')
        items=conn.execute("SELECT * FROM items WHERE public_visible=1 AND status NOT IN ('retired','quarantine','lost') ORDER BY category,name").fetchall()
        if any(not item_id_valid(it['id']) for it in items):
            raise ValueError('Unsafe item id')
        approved={}
        for r in conn.execute('SELECT * FROM public_assets'):
            path=Path(r['path']);src=root/path
            if path.is_absolute() or path.parts[:1]!=('photos',) or '..' in path.parts or src.is_symlink():
                continue
            if src.resolve().parent!=(root/'photos').resolve() or not src.is_file():
                continue
            if hashlib.sha256(src.read_bytes()).hexdigest()!=r['sha256']:
                continue
            approved[r['path']]=src
            conn.execute('INSERT INTO approved VALUES (?)',(r['path'],))
        groups={}
        for it in items:groups.setdefault(it['category'] or 'other',[]).append(it)
        order=[c for c in CATEGORY_LABELS if c in groups]+[c for c in groups if c not in CATEGORY_LABELS]
        out.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.catalog-',dir=out.parent) as stage_name:
            stage=Path(stage_name)
            for d in ('assets','photos','item'):(stage/d).mkdir()
            (stage/'index.html').write_text(_index_html(conn,groups,order,len(items)),encoding='utf-8')
            (stage/'assets/style.css').write_text(Path(__file__).with_name('catalog.css').read_text(),encoding='utf-8')
            shutil.copyfile(Path(__file__).with_name('catalog.js'),stage/'assets/catalog.js')
            needed=set()
            for it in items:
                (stage/'item'/f'{it["id"]}.html').write_text(_item_html(conn,it),encoding='utf-8')
                needed.update(_photos(conn,it['id']))
            for photo in needed:shutil.copyfile(approved[photo],stage/photo)
            (stage/'.nojekyll').touch()
            base=os.environ.get('CATALOG_BASE_PATH','/zavhoz-web/').rstrip('/')+'/'
            if not base.startswith('/') or any(c in base for c in '<>"\''):
                raise ValueError('Unsafe base path')
            (stage/'404.html').write_text(_head('Страница не найдена',base)+f'<main id="main"><h1>Страница не найдена</h1><p><a href="{E(base)}">Вернуться в каталог</a></p></main></body></html>',encoding='utf-8')
            out.mkdir(parents=True,exist_ok=True)
            # Complete build first, then replace generated paths. Keep .git/README.
            for name in ('assets','photos','item','index.html','404.html','.nojekyll'):
                target=out/name
                if target.is_symlink():raise ValueError('Symlink output path')
            for name in ('assets','photos','item','index.html','404.html','.nojekyll'):
                target=out/name
                if target.is_dir():shutil.rmtree(target)
                (stage/name).replace(target)
        return len(items)
    finally:
        conn.close()


if __name__=='__main__':
    import sys
    output=sys.argv[1] if len(sys.argv)>1 else 'site'
    print(f'site built: {build(output)} items -> {output}')
