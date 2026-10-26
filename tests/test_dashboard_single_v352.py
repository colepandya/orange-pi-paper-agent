import importlib.util,os,sqlite3,tempfile,unittest
ROOT=os.path.abspath(os.path.join(os.path.dirname(__file__),'..'));S=importlib.util.spec_from_file_location('d',os.path.join(ROOT,'app','dashboard_single_v352.py'));d=importlib.util.module_from_spec(S);S.loader.exec_module(d)
class Dashboard(unittest.TestCase):
 def fixture(self):
  t=tempfile.TemporaryDirectory();p=os.path.join(t.name,'x.db');x=sqlite3.connect(p);x.executescript("""create table ledger(id integer primary key,balance real);insert into ledger values(1,200000);create table positions(id integer primary key,ticker text,theme text,opened text,entry_price real,shares integer,cost real,decision_id integer,status text);insert into positions values(1,'MSFT','Technology','2026-10-26T02:30:34+00:00',500,100,50000,1,'OPEN');create table decisions(id integer primary key,created text,news_uid text,ticker text,action text,score real,reason text,rules_version text,status text,entry_price real,shares integer,amount real,theme text);insert into decisions values(1,'2026-10-26T02:30:00+00:00',null,'MSFT','BUY',85,'reason','3.4','EXECUTED',500,100,50000,'Technology');create table news(uid text,source text,title text,summary text,url text,published text);create table theses(decision_id integer,event_type text,thesis_json text,model_version text);insert into theses values(1,'product_customer','{}','3.4');create table event_exit_rules(event_type text,max_days integer,profit_target real,stop_loss real);insert into event_exit_rules values('product_customer',10,.15,.10);create table quote_cache(ticker text primary key,price real,source text,quoted_at text,updated_epoch real);""");x.commit();x.close();return t,p
 def test_nz_systemd_time(self):self.assertIn('NZDT',d.nz('Mon 2026-10-26 02:50:20 UTC'))
 def test_snapshot_positions(self):
  t,p=self.fixture();s=d.snapshot(p,refresh=False);self.assertEqual(len(s['positions']),1);self.assertEqual(s['cost'],50000);self.assertEqual(s['equity'],250000);t.cleanup()
 def test_single_page_required_sections(self):
  t,p=self.fixture();s=d.snapshot(p,refresh=False);h=d.render(s,dict(limit=100,ticker='',status='',source='',event=''))
  for text in ['Open virtual positions','Total capital buy price','Unrealised P&amp;L','Last collector','Next collector','Decision ledger','Active strategy','Learning and challengers','Operations, readiness and reconciliation']:self.assertIn(text,h)
  t.cleanup()
 def test_default_and_all_limits(self):
  t,p=self.fixture();h=d.render(d.snapshot(p,refresh=False),dict(limit=100,ticker='',status='',source='',event=''));self.assertIn('100 decisions</option>',h)
  for n in [50,100,250,500]:self.assertIn(f'value="{n}"',h)
  t.cleanup()
 def test_inline_audit(self):
  t,p=self.fixture();h=d.render(d.snapshot(p,refresh=False),dict(limit=100,ticker='',status='',source='',event=''));self.assertIn('Inspect decision #1',h);self.assertIn('Decision logic',h);self.assertIn('Thesis / version',h);t.cleanup()
 def test_capital_emphasis(self):self.assertIn('.card.highlight',d.CSS);self.assertIn('--gold:',d.CSS)
 def test_green_red_reserved(self):self.assertIn('.pos{color:var(--green)',d.CSS);self.assertIn('.neg{color:var(--red)',d.CSS)
 def test_one_page_anchors(self):
  t,p=self.fixture();h=d.render(d.snapshot(p,refresh=False),dict(limit=100,ticker='',status='',source='',event=''))
  for anchor in ['#portfolio','#positions','#decisions','#strategy','#learning','#operations']:self.assertIn(anchor,h)
  t.cleanup()
if __name__=='__main__':unittest.main()
