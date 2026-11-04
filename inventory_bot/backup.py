"""Consistent SQLite backups and lossless private inventory exports."""
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from datetime import datetime, timezone

TABLES=('items','projects','item_tags','item_photos','item_manuals','item_sources','item_knowledge','item_usage','public_assets','purchase_events')


def atomic_text(path, text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.'+path.name)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            f.write(text);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def export_snapshot(conn, path):
    payload={'version':2,'tables':{t:[dict(r) for r in conn.execute(f'SELECT * FROM {t}')] for t in TABLES}}
    atomic_text(path,json.dumps(payload,ensure_ascii=False,indent=2)+'\n')


def restore_snapshot(conn,path):
    data=json.loads(Path(path).read_text())
    if data.get('version')!=2 or set(data['tables'])!=set(TABLES):raise ValueError('Unknown export schema')
    if any(conn.execute(f'SELECT 1 FROM {t} LIMIT 1').fetchone() for t in TABLES):raise ValueError('Restore requires an empty database')
    with conn:
        for t in TABLES:
            columns={r['name'] for r in conn.execute(f'PRAGMA table_info({t})')}
            for row in data['tables'][t]:
                if set(row)-columns:raise ValueError('Unknown columns')
                keys=list(row)
                conn.execute(f'INSERT INTO {t} ({",".join(keys)}) VALUES ({",".join("?" for _ in keys)})',list(row.values()))
        if conn.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('Invalid foreign keys')


def sqlite_backup(source,destination):
    dest=Path(destination)
    if dest.exists():raise FileExistsError(dest)
    dest.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=dest.parent,prefix='.backup-');os.close(fd)
    src=sqlite3.connect(f'file:{Path(source).absolute()}?mode=ro',uri=True)
    copy=sqlite3.connect(tmp)
    try:
        src.backup(copy)
        if copy.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Backup integrity failed')
        copy.close();os.chmod(tmp,0o600);os.replace(tmp,dest)
    finally:
        src.close();copy.close()
        if os.path.exists(tmp):os.unlink(tmp)


if __name__=='__main__':
    import sys
    source=os.environ.get('INVENTORY_DB','inventory_bot/inventory.db')
    destination=sys.argv[1]
    sqlite_backup(source,destination)
    print('Backup verified:',destination)
