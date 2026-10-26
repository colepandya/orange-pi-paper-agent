import importlib.util,json,os,sqlite3,tempfile,unittest
P=os.path.join(os.path.dirname(__file__),'..','app','broad_collector.py'); s=importlib.util.spec_from_file_location('b',P); b=importlib.util.module_from_spec(s); s.loader.exec_module(b)
class T(unittest.TestCase):
 def test_xml(self):
  x=b.xml_items(b'<rss><channel><item><title>Acme wins contract</title><link>https://x</link><description>will launch</description></item></channel></rss>'); self.assertEqual(x[0]['title'],'Acme wins contract'); self.assertEqual(x[0]['url'],'https://x')
 def test_atom(self):
  x=b.xml_items(b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>8-K - Acme</title><link href="https://x"/><summary>contract</summary></entry></feed>'); self.assertEqual(len(x),1)
 def test_schema(self):
  with tempfile.TemporaryDirectory() as t:
   d=sqlite3.connect(os.path.join(t,'x.db')); b.init(d); self.assertEqual(d.execute("select count(*) from sqlite_master where name='source_health'").fetchone()[0],1)
 def test_config_keyless(self):
  
  with open(os.path.join(os.path.dirname(__file__),'..','config','sources.json')) as f: c=json.load(f); raw=json.dumps(c).lower(); self.assertNotIn('api_key',raw); self.assertNotIn('token',raw); self.assertGreaterEqual(len(c['sources']),13)
 def test_backoff(self):
  with tempfile.TemporaryDirectory() as t:
   d=sqlite3.connect(os.path.join(t,'x.db')); b.init(d); src={'id':'x','name':'X'}; b.health(d,src,429,Exception('rate')); r=d.execute('select failures,backoff_until from source_health').fetchone(); self.assertEqual(r[0],1); self.assertGreater(r[1],0)
if __name__=='__main__': unittest.main()
