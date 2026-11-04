import json
import os
import subprocess
from .backup import atomic_text, export_snapshot

try:
    from .storage import connect, utc_now
except ImportError:
    from storage import connect, utc_now


def q(value) -> str:
    text = "" if value is None else str(value)
    return json.dumps(text, ensure_ascii=False)


def export_items(repo_dir: str) -> None:
    conn = connect()
    try:
        conn.execute("BEGIN")
        rows = conn.execute("SELECT * FROM items ORDER BY id").fetchall()
        out = []
        out.append("version: 1\n")
        out.append(f"updated: {q(utc_now()[:10])}\n")
        out.append("currency: RUB\n")
        out.append("locations:\n")
        out.append("  - id: unsorted\n")
        out.append("    name: \"Unsorted / needs physical box label\"\n")
        out.append("    notes: \"Temporary location until storage boxes are labeled.\"\n\n")
        out.append("items:\n")
        for row in rows:
            photos = conn.execute("SELECT path FROM item_photos WHERE item_id = ? ORDER BY path", (row["id"],)).fetchall()
            manuals = conn.execute("SELECT url_or_path FROM item_manuals WHERE item_id = ? ORDER BY url_or_path", (row["id"],)).fetchall()
            usage = conn.execute("SELECT * FROM item_usage WHERE item_id = ? ORDER BY id", (row["id"],)).fetchall()
            tags = conn.execute("SELECT tag FROM item_tags WHERE item_id = ? ORDER BY tag", (row["id"],)).fetchall()
            sources = conn.execute("SELECT * FROM item_sources WHERE item_id = ? ORDER BY kind, url", (row["id"],)).fetchall()
            knowledge = conn.execute("SELECT * FROM item_knowledge WHERE item_id = ?", (row["id"],)).fetchone()
            out.append(f"  - id: {q(row['id'])}\n")
            out.append(f"    name: {q(row['name'])}\n")
            out.append(f"    category: {q(row['category'])}\n")
            out.append(f"    status: {q(row['status'])}\n")
            out.append(f"    total_qty: {row['total_qty']:g}\n")
            out.append(f"    available_qty: {row['available_qty']:g}\n")
            out.append(f"    unit: {q(row['unit'])}\n")
            out.append(f"    location: {q(row['location'])}\n")
            out.append(f"    price_rub: {row['price_rub']:g}\n")
            out.append(f"    description: {q(row['description'])}\n")
            out.append(f"    last_verified_at: {q(row['last_verified_at'])}\n")
            out.append("    purchase: null\n")
            out.append("    photos:\n")
            if photos:
                for photo in photos:
                    out.append(f"      - {q(photo['path'])}\n")
            else:
                out.append("      []\n")
            out.append("    manuals:\n")
            if manuals:
                for manual in manuals:
                    out.append(f"      - {q(manual['url_or_path'])}\n")
            else:
                out.append("      []\n")
            out.append("    used_in:\n")
            if usage:
                for use in usage:
                    out.append(f"      - project_id: {q(use['project_id'])}\n")
                    out.append(f"        qty: {use['qty']:g}\n")
                    out.append(f"        role: {q(use['role'])}\n")
                    out.append(f"        since: {q(use['since'])}\n")
                    out.append(f"        removable: {str(bool(use['removable'])).lower()}\n")
            else:
                out.append("      []\n")
            out.append("    tags:")
            if tags:
                out.append(" [" + ", ".join(q(tag["tag"]) for tag in tags) + "]\n")
            else:
                out.append(" []\n")
            out.append("    sources:\n")
            if sources:
                for source in sources:
                    out.append(f"      - kind: {q(source['kind'])}\n")
                    out.append(f"        title: {q(source['title'])}\n")
                    out.append(f"        url: {q(source['url'])}\n")
                    out.append(f"        notes: {q(source['notes'])}\n")
            else:
                out.append("      []\n")
            out.append("    knowledge:\n")
            if knowledge:
                out.append(f"      summary: {q(knowledge['summary'])}\n")
                out.append(f"      specs_json: {q(knowledge['specs_json'])}\n")
            else:
                out.append("      null\n")
            out.append(f"    notes: {q(row['notes'])}\n\n")

        os.makedirs(repo_dir, exist_ok=True)
        atomic_text(os.path.join(repo_dir, "items.yaml"), "".join(out))
        export_snapshot(conn, os.path.join(repo_dir, "inventory.json"))
        atomic_text(os.path.join(repo_dir, "projects.yaml"), json.dumps({"projects": [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id")]}, ensure_ascii=False, indent=2))
    finally:
        conn.close()

    atomic_text(os.path.join(repo_dir, "ai-context.md"), """# Склад электроники

Источник текущего состояния — рабочая SQLite-база бота. Этот каталог — приватный экспорт.
`inventory.json` версии 2 содержит полный экспорт складских таблиц для восстановления;
`items.yaml` — читаемое представление, не резервная копия всей БД.
`projects.yaml` содержит актуальные проекты. SQLite backup сохраняет также черновики и сессии.

Количество с quantity_basis=purchase получено из закупки, физически не пересчитано.
Ожидаемые поставки (ordered) и ошибочные поставки (quarantine) не являются свободным запасом.
Не заменять фактическую модель названием заказанного товара при несоответствии поставки.
Не публиковать этот репозиторий, источники покупок, остатки и фотографии без проверки.
""")


def maybe_git_sync(repo_dir: str, message: str) -> None:
    if os.environ.get("INVENTORY_AUTO_GIT", "0") != "1":
        return
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=repo_dir, check=True, capture_output=True, text=True, timeout=15).stdout.strip()
    if not remote.endswith((":azbezd/zavhoz-inventory.git", "/azbezd/zavhoz-inventory.git")):
        raise RuntimeError("Git export is restricted to the private inventory repository")
    subprocess.run(["git", "add", "--", "items.yaml", "projects.yaml", "inventory.json", "ai-context.md", "photos"], cwd=repo_dir, check=True, timeout=30)
    commit = subprocess.run(
        ["git", "commit", "-m", f"inventory: {message}"],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr):
        raise RuntimeError(commit.stdout + commit.stderr)
    subprocess.run(["git", "push"], cwd=repo_dir, check=True, timeout=60)


if __name__ == "__main__":
    export_items(os.environ.get("INVENTORY_REPO_DIR", os.getcwd()))
