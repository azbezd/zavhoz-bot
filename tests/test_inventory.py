import hashlib,json,os,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from inventory_bot import storage as st
from inventory_bot.import_purchases import import_rows
from inventory_bot.backup import export_snapshot,restore_snapshot,sqlite_backup,TABLES
from inventory_bot.build_site import build
from inventory_bot.safety import download_public,quantity

class InventoryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.env=patch.dict(os.environ,{'INVENTORY_DB':str(self.root/'test.db'),'INVENTORY_REPO_DIR':str(self.root/'private')})
  self.env.start();self.conn=st.connect();st.seed_projects(self.conn)
 def tearDown(self):self.conn.close();self.env.stop();self.tmp.cleanup()
 def proposal(self,ops,user=1):return st.save_proposal(self.conn,user,1,'test',[],{'operations':ops})
 def add(self,qty=5):
  r=st.apply_proposal(self.conn,self.proposal([{'op':'add_item','name':'Test','qty':qty}]),user_id=1)
  return r['applied'][0]['item_id']
 def test_failed_proposal_rolls_back_every_operation(self):
  pid=self.proposal([{'op':'add_item','name':'Never inserted','qty':2},{'op':'mark_used','item_id':'missing','qty':1}])
  with self.assertRaises(ValueError):st.apply_proposal(self.conn,pid)
  st.set_preference(self.conn,'after_error','commit')
  self.assertEqual(self.conn.execute('SELECT count(*) FROM items').fetchone()[0],0)
  self.assertEqual(st.get_proposal(self.conn,pid)['status'],'pending')
  self.assertFalse(self.conn.in_transaction)
 def test_ownership_and_replay(self):
  pid=self.proposal([{'op':'add_item','qty':1}])
  with self.assertRaises(ValueError):st.apply_proposal(self.conn,pid,user_id=2)
  self.assertIsNone(st.get_proposal(self.conn,pid,user_id=2))
  self.assertFalse(st.discard_proposal(self.conn,pid,user_id=2))
  st.apply_proposal(self.conn,pid,user_id=1)
  with self.assertRaises(ValueError):st.apply_proposal(self.conn,pid,user_id=1)
 def test_never_accepts_path_from_model(self):
  result=st.apply_proposal(self.conn,self.proposal([{'op':'add_item','item_id':'../../evil','qty':1}]))
  self.assertTrue(result['applied'][0]['item_id'].startswith('hw-'))
 def test_quantity_validation_and_overspend(self):
  for v in [-1,float('inf'),float('nan')]:
   with self.subTest(v=v),self.assertRaises(ValueError):quantity(v)
  iid=self.add(2)
  for op in [{'op':'adjust_qty','item_id':iid,'qty':-3},{'op':'mark_used','item_id':iid,'qty':3}]:
   with self.assertRaises((ValueError,sqlite3.IntegrityError)):st.apply_proposal(self.conn,self.proposal([op]))
   self.assertEqual(st.get_item(self.conn,iid)['available_qty'],2)
 def test_split_preserves_allocations_and_metadata(self):
  iid=self.add(5);self.conn.execute("INSERT INTO item_manuals VALUES (?,?,'Manual')",(iid,'https://example.com'))
  self.conn.execute("INSERT INTO item_knowledge VALUES (?,'known','{}','now','reviewed')",(iid,));self.conn.commit()
  st.apply_proposal(self.conn,self.proposal([{'op':'mark_used','item_id':iid,'qty':2,'project_id':'project-netbox'}]))
  new,remaining=st.item_split_to_project(self.conn,iid,1,'project-freenet')
  self.assertEqual((remaining,st.get_item(self.conn,iid)['available_qty']),(4,2))
  self.assertIsNotNone(self.conn.execute('SELECT 1 FROM item_knowledge WHERE item_id=?',(new,)).fetchone())
  with self.assertRaises(ValueError):st.inv_mark_qty(self.conn,iid,1)
 def test_retired_not_resurrected(self):
  iid=self.add(1);st.item_set_in_project(self.conn,iid,'project-netbox');st.item_retire(self.conn,iid)
  st.project_delete(self.conn,'project-netbox')
  self.assertEqual(st.get_item(self.conn,iid)['status'],'retired')
 def test_multiple_project_removal_releases_only_that_quantity(self):
  iid=self.add(5)
  for pid in ['project-netbox','project-freenet']:
   st.apply_proposal(self.conn,self.proposal([{'op':'mark_used','item_id':iid,'qty':1,'project_id':pid}]))
  st.project_remove_item(self.conn,'project-netbox',iid)
  self.assertEqual(st.get_item(self.conn,iid)['available_qty'],4)
 def test_reference_does_not_destroy_purchase_or_reviewed_knowledge(self):
  iid=self.add();st.item_set_source(self.conn,iid,'https://ozon.ru/product/123')
  st.item_set_source(self.conn,iid,'https://amperkot.ru/product/456',kind='reference')
  self.assertEqual(self.conn.execute('SELECT count(*) FROM item_sources').fetchone()[0],2)
  self.conn.execute("INSERT INTO item_knowledge VALUES (?,'verified','{}','now','reviewed')",(iid,));self.conn.commit()
  st.item_apply_enrichment(self.conn,iid,{'summary':'guessed'})
  self.assertEqual(self.conn.execute('SELECT summary FROM item_knowledge').fetchone()[0],'verified')
 def test_manifest_idempotent_and_pending_not_available(self):
  r={'event_key':'one','sku':'1','source_url':'https://ozon.ru/product/1','name':'part','category':'module','packages':2,'pack_size':5,'date':'2026-11-04','state':'received'}
  pending=dict(r,event_key='two',state='ordered');bad=dict(r,event_key='three',state='quarantine')
  result=import_rows(self.conn,[r,pending,bad]);self.assertEqual(len(result),3)
  self.assertEqual(import_rows(self.conn,[r,pending,bad]),[])
  self.assertEqual([st.get_item(self.conn,x['item_id'])['available_qty'] for x in result],[10,0,0])
  with self.assertRaises(ValueError):import_rows(self.conn,[dict(r,packages=3)])
 def test_backup_with_wal_and_roundtrip(self):
  self.add();backup=self.root/'backup.sqlite';sqlite_backup(os.environ['INVENTORY_DB'],backup)
  other=sqlite3.connect(backup);self.assertEqual(other.execute('SELECT count(*) FROM items').fetchone()[0],1);other.close()
  export=self.root/'inventory.json';export_snapshot(self.conn,export)
  os.environ['INVENTORY_DB']=str(self.root/'restored.db');other=st.connect();restore_snapshot(other,export)
  for table in TABLES:self.assertEqual([dict(r) for r in self.conn.execute(f'SELECT * FROM {table}')],[dict(r) for r in other.execute(f'SELECT * FROM {table}')])
  other.close()
 def test_build_omits_private_and_unapproved_bytes_and_stale_files(self):
  iid=self.add();private=self.root/'private';(private/'photos').mkdir(parents=True)
  for path,data in [('public.jpg',b'public'),('order.jpg',b'PRIVATE ORDER')]:
   (private/'photos'/path).write_bytes(data);self.conn.execute('INSERT INTO item_photos VALUES (?,?)',(iid,'photos/'+path))
  self.conn.execute('INSERT INTO public_assets VALUES (?,?,?,?)',('photos/public.jpg',hashlib.sha256(b'public').hexdigest(),'now','product'))
  self.conn.execute("UPDATE items SET notes='SECRET', price_rub=999, location='PRIVATE ADDRESS' WHERE id=?",(iid,))
  self.conn.execute('INSERT INTO item_manuals VALUES (?, ?, ?)',(iid,'javascript:alert(1)','dangerous'))
  self.conn.execute("INSERT INTO item_knowledge VALUES (?,'test','[]','now','')",(iid,));self.conn.commit()
  out=self.root/'site';(out/'item').mkdir(parents=True);(out/'item/stale.html').write_text('old')
  build(str(out));self.assertFalse((out/'item/stale.html').exists());self.assertFalse((out/'photos/order.jpg').exists())
  self.assertEqual((out/'photos/public.jpg').read_bytes(),b'public')
  text=''.join(p.read_text() for p in out.rglob('*.html'))
  for secret in ['PRIVATE ADDRESS','SECRET','javascript:','PRIVATE ORDER']:self.assertNotIn(secret,text)
  (private/'photos/public.jpg').write_bytes(b'changed unreviewed');build(str(out))
  self.assertFalse((out/'photos/public.jpg').exists())
 def test_ssrf_blocks_private_ipv4_ipv6(self):
  for address in ['127.0.0.1','10.1.2.3','169.254.169.254','::1','fd00::1']:
   with patch('socket.getaddrinfo',return_value=[(2,1,6,'',(address,443))]),self.assertRaises(ValueError):download_public('https://example.com/image')

if __name__=='__main__':unittest.main()
