import importlib.util,os,sqlite3,tempfile,unittest
ROOT=os.path.abspath(os.path.join(os.path.dirname(__file__),'..'));sp=importlib.util.spec_from_file_location('v3',os.path.join(ROOT,'app','strategy_v3.py'));v3=importlib.util.module_from_spec(sp);sp.loader.exec_module(v3)
class T(unittest.TestCase):
 def db(self):
  t=tempfile.TemporaryDirectory();d=sqlite3.connect(os.path.join(t.name,'x'));d.row_factory=sqlite3.Row;v3.init(d);return t,d
 def test_high_quality_contract(self):
  t,d=self.db();r=v3.evaluate(d,'AMTM',{'name':'Amentum Holdings Inc','aliases':['Amentum Technology']},'USAspending New Awards',.95,'Amentum Technology wins $500 million contract','The company will perform the order','2026-10-25T00:00:00+00:00','2026-10-25T01:00:00+00:00');self.assertGreaterEqual(r['score'],65);self.assertEqual(r['event_type'],'government_contract');t.cleanup()
 def test_stale_hard_cap(self):
  t,d=self.db();r=v3.evaluate(d,'AMTM',{'name':'Amentum Holdings Inc','aliases':['Amentum Technology']},'USAspending New Awards',.95,'Amentum Technology wins $500 million contract','will perform','2026-10-01T00:00:00+00:00','2026-10-25T01:00:00+00:00');self.assertLess(r['score'],65);self.assertEqual(r['stale'],1);t.cleanup()
 def test_short_ticker_not_enough(self):
  t,d=self.db();r=v3.evaluate(d,'A',{'name':'Agilent Technologies Inc','aliases':['Agilent Technologies']},'TechCrunch',.7,'A new technology launches','A company will grow','2026-10-25T00:00:00+00:00','2026-10-25T01:00:00+00:00');self.assertLess(r['components'][0],16);self.assertLess(r['score'],65);t.cleanup()
 def test_duplicate_hard_cap(self):
  t,d=self.db();r=v3.evaluate(d,'X',{'name':'Example Holdings Inc','aliases':['Example Holdings']},'SEC EDGAR Latest Filings',1,'Example Holdings wins $1 billion award','will launch','2026-10-25T00:00:00+00:00','2026-10-25T01:00:00+00:00');d.execute("create table decisions(id integer primary key)");d.execute('insert into decisions values(1)');v3.record(d,1,r);r2=v3.evaluate(d,'X',{'name':'Example Holdings Inc','aliases':['Example Holdings']},'SEC EDGAR Latest Filings',1,'Example Holdings wins $1 billion award','will launch','2026-10-25T00:00:00+00:00','2026-10-25T01:01:00+00:00');self.assertEqual(r2['duplicate'],1);self.assertLess(r2['score'],65);t.cleanup()
 def test_model_hash_and_schema(self):
  t,d=self.db();self.assertEqual(d.execute('select count(*) from model_versions').fetchone()[0],1);self.assertEqual(v3.PARAMETERS['entry_score'],65);self.assertEqual(v3.PARAMETERS['allocations']['65-79'],.10);t.cleanup()
if __name__=='__main__':unittest.main()
