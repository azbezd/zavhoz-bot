"""Apply a reviewed, private purchase manifest once. No network or AI calls."""
import json
import hashlib
from .storage import connect, utc_now, next_item_id, atomic
from .safety import quantity, web_url


@atomic
def import_rows(conn, rows):
    result=[]
    for r in rows:
        key=r['event_key']
        old=conn.execute('SELECT * FROM purchase_events WHERE event_key=?',(key,)).fetchone()
        if old:
            if json.loads(old['evidence_json'])!=r:
                raise ValueError('Evidence changed for already imported event: '+key)
            continue
        packages=quantity(r['packages'],positive=True);pack=quantity(r['pack_size'],positive=True)
        qty=quantity(packages*pack,positive=True)
        if r['state'] not in ('received','ordered','quarantine') or not web_url(r['source_url']):
            raise ValueError('Invalid purchase state or URL')
        now=utc_now();iid=next_item_id(conn)
        available=qty if r['state']=='received' else 0
        status=('tool' if r['category']=='tool' else 'stock') if r['state']=='received' else r['state']
        note=f"Ozon: {r['date']}; {packages:g} уп. × {pack:g} = {qty:g} {r.get('unit','pcs')}. Количество по закупке; физический остаток и использование в проектах не проверены."
        if r.get('note'):note+=' '+r['note']
        conn.execute('''INSERT INTO items (id,name,category,status,total_qty,available_qty,unit,notes,description,quantity_basis,public_visible,created_at,updated_at)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                     (iid,r['name'],r['category'],status,qty,available,r.get('unit','pcs'),note,r.get('description',''),'purchase',0 if status=='quarantine' else 1,now,now))
        conn.execute('INSERT INTO item_sources(item_id,kind,title,url,notes) VALUES (?,?,?,?,?)',
                     (iid,'purchase','Ozon',r['source_url'],r.get('listing_name','')))
        conn.execute('INSERT INTO item_knowledge(item_id,summary,specs_json,updated_at,reviewed_at) VALUES (?,?,?,?,?)',
                     (iid,r.get('summary',''),json.dumps(r.get('specs',{}),ensure_ascii=False),now,now))
        for doc in r.get('docs',[]):
            if not web_url(doc['url']):raise ValueError('Unsafe documentation URL')
            conn.execute('INSERT INTO item_manuals(item_id,url_or_path,title) VALUES (?,?,?)',(iid,doc['url'],doc['title']))
        if r.get('photo'):
            conn.execute('INSERT INTO item_photos VALUES (?,?)',(iid,r['photo']))
        conn.execute('INSERT INTO purchase_events VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                     (key,iid,r['source_url'],r['sku'],r['date'],packages,pack,qty,r['state'],json.dumps(r,ensure_ascii=False,sort_keys=True),now))
        result.append({'item_id':iid,'name':r['name'],'qty':qty,'state':r['state']})
    return result


if __name__=='__main__':
    import sys
    rows=json.load(open(sys.argv[1],encoding='utf-8'))
    conn=connect()
    try:
        print(json.dumps(import_rows(conn,rows),ensure_ascii=False,indent=2))
    finally:conn.close()
