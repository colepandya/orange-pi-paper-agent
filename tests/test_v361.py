import os,sqlite3,tempfile,unittest
from pathlib import Path
from app import impact_v36 as impact
from app import position_plans_v361 as plans
ROOT=os.path.abspath(os.path.join(os.path.dirname(__file__),'..'))
class V361(unittest.TestCase):
 def db(self):
  t=tempfile.TemporaryDirectory();d=impact.connect(os.path.join(t.name,'x.db'));d.executescript("""create table decisions(id integer primary key,created text,news_uid text,ticker text,action text,score real,reason text,rules_version text,status text,entry_price real,shares integer,amount real,theme text);create table news(uid text,source text,title text,summary text,url text,published text);create table theses(decision_id integer,event_type text,thesis_json text,model_version text);create table positions(id integer primary key,ticker text,theme text,opened text,entry_price real,shares integer,cost real,decision_id integer,status text);create table ledger(id integer primary key,balance real);insert into ledger values(1,900000);create table company_fundamentals(ticker text primary key,cik text,revenue real,operating_income real,cash real,debt real,shares real,market_cap real,backlog real,as_of text,source_url text);insert into company_fundamentals values('TEST','1',1000000000,100000000,1,1,10000000,2000000000,400000000,'2026-01-01','SEC');insert into news values('n','USAspending New Awards','TEST wins $500 million funded contract','signed award','u','2026-01-01');insert into decisions values(1,'2026-01-01','n','TEST','BUY',85,'recorded reason','3.6','EXECUTED',100,100,10000,'Defense');insert into theses values(1,'government_contract','{"source_quality":15,"entity_evidence":20,"novelty":15}','3.0');insert into positions values(1,'TEST','Defense','2026-01-01T00:00:00+00:00',100,100,10000,1,'OPEN');""");d.commit();return t,d
 def test_open_position_plan_dynamic(self):
  t,d=self.db();out=plans.refresh(d);self.assertEqual(len(out),1);r=d.execute('select * from position_forecast_plans where active=1').fetchone();self.assertNotEqual(r['target_return'],.15);self.assertNotEqual(abs(r['stop_return']),.10);self.assertNotEqual(r['max_hold_days'],10);self.assertAlmostEqual(r['target_price'],100*(1+r['target_return']));t.cleanup()
 def test_plan_history_retained(self):
  t,d=self.db();plans.refresh(d);plans.refresh(d);self.assertEqual(d.execute('select count(*) from position_forecast_plans').fetchone()[0],2);self.assertEqual(d.execute('select count(*) from position_forecast_plans where active=1').fetchone()[0],1);t.cleanup()
 def test_forecast_hash_and_rationale(self):
  t,d=self.db();plans.refresh(d);r=d.execute('select * from position_forecast_plans').fetchone();self.assertEqual(len(r['forecast_hash']),64);self.assertIn('residual opportunity',r['rationale']);t.cleanup()
 def test_dashboard_inline_not_separate(self):
  s=Path(os.path.join(ROOT,'app','dashboard_v361.py')).read_text(encoding='utf-8');self.assertIn('impact forecast included in every line item',s);self.assertIn('Inspect evidence, decision logic and impact forecast',s);self.assertNotIn('def exposure_panel',s);self.assertNotIn('def forecast_panel',s)
 def test_no_raw_json_rendering(self):
  s=Path(os.path.join(ROOT,'app','dashboard_v361.py')).read_text(encoding='utf-8');self.assertNotIn('json.dumps(json.loads',s);self.assertIn('Why this plan',s);self.assertIn('Thesis invalidation',s)
 def test_port_8088_health(self):
  s=Path(os.path.join(ROOT,'app','dashboard_v361.py')).read_text(encoding='utf-8');self.assertIn("'port':8088",s)
if __name__=='__main__':unittest.main()
