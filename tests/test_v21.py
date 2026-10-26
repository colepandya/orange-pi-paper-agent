import datetime as dt,importlib.util,json,os,sqlite3,tempfile,time,unittest
from unittest.mock import patch
ROOT=os.path.abspath(os.path.join(os.path.dirname(__file__),'..'))
spec=importlib.util.spec_from_file_location('b',os.path.join(ROOT,'app','broad_collector.py')); b=importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
class SourceParsers(unittest.TestCase):
 def test_rss_and_atom(self):
  rss=b.xml_items(b'<rss><channel><item><title>Acme award</title><link>https://x</link><description>will launch</description><pubDate>now</pubDate></item></channel></rss>'); self.assertEqual(rss[0]['url'],'https://x')
  atom=b.xml_items(b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>8-K - ACME</title><link href="https://x/8k"/><summary>contract</summary><updated>2026-01-01T00:00:00Z</updated></entry></feed>'); self.assertEqual(atom[0]['title'],'8-K - ACME'); self.assertEqual(atom[0]['url'],'https://x/8k')
 def fake(self,obj,status=200): return json.dumps(obj).encode(),status
 def test_federal_register(self):
  src={'id':'f','type':'federal_register','url':'x'}
  with patch.object(b,'req',return_value=self.fake({'results':[{'title':'Export rule','abstract':'chips','document_type':'Notice','html_url':'https://fr','filed_at':'2026-01-01','agencies':[{'name':'Commerce'}]}]})):
   x,st=b.fetch_source(src); self.assertEqual(st,200); self.assertIn('Commerce',x[0]['summary']); self.assertEqual(x[0]['url'],'https://fr')
 def test_openfda(self):
  src={'id':'o','type':'openfda','url':'x'}
  with patch.object(b,'req',return_value=self.fake({'results':[{'recalling_firm':'Acme','product_description':'Device','reason_for_recall':'fault','classification':'I','status':'Ongoing','report_date':'20261001'}]})):
   x,st=b.fetch_source(src); self.assertIn('Acme',x[0]['title']); self.assertIn('fault',x[0]['summary'])
 def test_usaspending_payload_and_parser(self):
  src={'id':'u','type':'usaspending','url':'x'}; seen={}
  def fake(url,data=None,headers=None,timeout=20): seen['payload']=json.loads(data); return self.fake({'results':[{'Award ID':'CONT123','Recipient Name':'Acme Corp','Start Date':'2026-10-20','Award Amount':50000000,'Awarding Agency':'Defense','Description':'radar'}]})
  with patch.object(b,'req',side_effect=fake):
   x,st=b.fetch_source(src); self.assertEqual(seen['payload']['filters']['award_type_codes'],['A','B','C','D']); self.assertIn('Acme Corp',x[0]['title']); self.assertIn('CONT123',x[0]['url'])
 def test_sec_form_filter(self):
  src={'id':'sec_latest','type':'atom','url':'x'}
  xml=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>8-K - ACME</title><link href="https://x/1"/></entry><entry><title>10-K - BETA</title><link href="https://x/2"/></entry><entry><title>UPLOAD - BAD</title><link href="https://x/3"/></entry></feed>'
  with patch.object(b,'req',return_value=(xml,200)):
   x,st=b.fetch_source(src); self.assertEqual([z['form'] for z in x],['8-K','10-K'])
class Scheduler(unittest.TestCase):
 def setUp(self): self.t=tempfile.TemporaryDirectory(); self.d=sqlite3.connect(os.path.join(self.t.name,'x.db')); b.init(self.d); self.src={'id':'s','name':'S','interval':900}
 def tearDown(self): self.d.close(); self.t.cleanup()
 def test_never_due(self): self.assertEqual(b.source_due(self.d,self.src,lambda:1000),(True,'never_run'))
 def test_interval_wait_and_due(self):
  when=dt.datetime.fromtimestamp(1000,dt.timezone.utc).isoformat(); self.d.execute("insert into source_health(source_id,name,last_attempt,backoff_until,items_total,failures) values(?,?,?,?,0,0)",('s','S',when,0)); self.d.commit(); self.assertEqual(b.source_due(self.d,self.src,lambda:1500)[1],'interval_wait'); self.assertEqual(b.source_due(self.d,self.src,lambda:2000)[1],'interval_due')
 def test_backoff_wins(self):
  when=dt.datetime.fromtimestamp(1000,dt.timezone.utc).isoformat(); self.d.execute("insert into source_health(source_id,name,last_attempt,backoff_until,items_total,failures) values(?,?,?,?,0,1)",('s','S',when,2500)); self.d.commit(); self.assertEqual(b.source_due(self.d,self.src,lambda:2000),(False,'backoff'))
 def test_health_backoff_and_reset(self):
  b.health(self.d,self.src,429,Exception('rate')); r=self.d.execute('select failures,backoff_until,last_error from source_health').fetchone(); self.assertEqual(r[0],1); self.assertGreater(r[1],time.time()); b.health(self.d,self.src,200,count=4); r=self.d.execute('select failures,backoff_until,items_total,last_error from source_health').fetchone(); self.assertEqual(r,(0,0.0,4,None))
class Safety(unittest.TestCase):
 def test_keyless_registry(self):
  
  with open(os.path.join(ROOT,'config','sources.json')) as f: c=json.load(f); raw=json.dumps(c).lower(); self.assertNotIn('api_key',raw); self.assertNotIn('bearer',raw); self.assertNotIn('oauth',raw); self.assertEqual(len({x['id'] for x in c['sources']}),len(c['sources'])); self.assertTrue(all(x['interval']>=300 for x in c['sources']))
 def test_schema_idempotent(self):
  with tempfile.TemporaryDirectory() as t:
   d=sqlite3.connect(os.path.join(t,'x')); b.init(d); b.init(d); self.assertEqual(d.execute("select count(*) from sqlite_master where name='source_health'").fetchone()[0],1)
 def test_uid_is_source_scoped(self):
  a=b.hashlib.sha256(('a|u|t').encode()).hexdigest(); z=b.hashlib.sha256(('b|u|t').encode()).hexdigest(); self.assertNotEqual(a,z)
if __name__=='__main__': unittest.main()
