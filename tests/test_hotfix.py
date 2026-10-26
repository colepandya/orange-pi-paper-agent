import importlib.util,json,os,sqlite3,tempfile,unittest,urllib.error
from unittest.mock import patch
ROOT=os.path.abspath(os.path.join(os.path.dirname(__file__),'..')); sp=importlib.util.spec_from_file_location('b',os.path.join(ROOT,'app','broad_collector.py')); b=importlib.util.module_from_spec(sp); sp.loader.exec_module(b)
class FakeStore:
 def __init__(self,path): self.db=sqlite3.connect(path); self.db.row_factory=sqlite3.Row; self.db.executescript("create table news(uid text unique,source,title,summary,url,published,ingested,trust); create table decisions(id integer primary key,created,news_uid,ticker,action,score,reason,rules_version,status,theme);")
 def rules_version(self): return 'x'
class Core:
 @staticmethod
 def map_tickers(blob,w): return ['ACME']
 @staticmethod
 def score_event(blob,t): return 95,['test']
 @staticmethod
 def direction(blob): return 'BUY'
 @staticmethod
 def execute_virtual_buy(*args): raise urllib.error.HTTPError('https://price',404,'not found',{},None)
class Hotfix(unittest.TestCase):
 def test_price_404_finalizes_decision(self):
  with tempfile.TemporaryDirectory() as t:
   st=FakeStore(os.path.join(t,'x')); cfg={'portfolio':{'entry_score':65}}; w={'ACME':{'theme':'X','name':'Acme Corporation','aliases':['Acme Corporation']}}; r=b.process(Core,st,cfg,w,{'id':'s','name':'SEC EDGAR Latest Filings','trust':1},{'title':'Acme Corporation wins $500 million contract','summary':'will launch','url':'u','published':'p'}); row=st.db.execute('select status from decisions').fetchone(); self.assertEqual(row[0],'NO_PRICE'); self.assertEqual(r['no_price'],1)
 def test_registry_six_verified(self):
  with open(os.path.join(ROOT,'config','sources.json')) as f:c=json.load(f)
  self.assertEqual(len(c['sources']),13); self.assertTrue({'sec_latest','federal_register_pi','usaspending_awards','ftc_press','openfda_drug','openfda_device','techcrunch','ars_tech','the_verge','techmeme','hacker_news','geekwire','nvidia_news'}.issubset({x['id'] for x in c['sources']}))
 def test_cycle_separates_fetch_and_item_errors_static(self):
  
  with open(os.path.join(ROOT,'app','broad_collector.py')) as f: s=f.read()
  self.assertIn("totals['fetch_failures']",s); self.assertIn("totals['item_failures']",s); self.assertIn("health(store.db,src,status,count=len(items))",s)
if __name__=='__main__':unittest.main()
