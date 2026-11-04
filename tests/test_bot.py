import os,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from inventory_bot import bot

class BotTests(unittest.TestCase):
 def test_long_html_and_emoji_delivered_without_truncation(self):
  text='Деталь 🔧 '*1700; calls=[]
  with patch.object(bot,'telegram',side_effect=lambda method,payload,**kw:(calls.append(payload) or {'result':{'message_id':1}})):
   bot.send(1,'<b>'+text+'</b>',parse_mode='HTML')
  self.assertEqual(''.join(p['text'] for p in calls),text)
  self.assertTrue(all(len(p['text'].encode('utf-16-le'))<=7000 for p in calls))
  self.assertTrue(all('parse_mode' not in p for p in calls))
 def test_local_photo_cannot_escape_private_photo_directory(self):
  with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'INVENTORY_REPO_DIR':d}),patch.object(bot,'_send_photo_file') as upload,patch.object(bot,'telegram') as api:
   photos=Path(d)/'photos';photos.mkdir();(photos/'escape.jpg').symlink_to('/etc/passwd')
   for name in ['../.env','/etc/passwd','photos/escape.jpg','photos/private.txt']:
    self.assertIsNone(bot.send_photo(1,name,'caption'))
   upload.assert_not_called();api.assert_not_called()
 def test_unlisted_user_and_group_never_reach_ai_or_send_message(self):
  with patch.dict(os.environ,{'ALLOWED_TELEGRAM_USER_IDS':'1'}),patch.object(bot,'send') as send,patch.object(bot,'telegram') as api:
   for user,kind in [(2,'private'),(1,'group')]:
    bot.handle_message(None,{'chat':{'id':1,'type':kind},'from':{'id':user},'text':'купил 20 модулей'})
   send.assert_not_called();api.assert_not_called()
 def test_quarantine_has_no_public_link_or_available_stock_claim(self):
  row={'id':'hw-2026-001','name':'Неизвестный датчик','unit':'pcs','total_qty':3,'available_qty':0,'status':'quarantine','quantity_basis':'purchase','public_visible':0}
  rendered=bot._item_li(row)
  self.assertNotIn('href=',rendered);self.assertIn('свободно 0',rendered)
 def test_telegram_errors_do_not_disclose_token_url(self):
  with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'TEST_SECRET'}),patch('urllib.request.urlopen',side_effect=RuntimeError('https://api.telegram.org/botTEST_SECRET/sendMessage')):
   with self.assertRaises(RuntimeError) as e:bot.telegram('sendMessage',{'text':'private'})
  self.assertNotIn('TEST_SECRET',str(e.exception));self.assertNotIn('private',str(e.exception))

if __name__=='__main__':unittest.main()
