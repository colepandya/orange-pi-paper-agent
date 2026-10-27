import datetime as dt,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from app import market_data_fabric_v402 as m
class V402(unittest.TestCase):
 def test_no_keys_fail_closed(self):
  with patch.dict('os.environ',{},clear=True):c=m.consensus('MSFT',dt.datetime(2026,10,27,14,tzinfo=dt.timezone.utc))
  self.assertEqual(c['status'],'FAILED');self.assertEqual(c['execution_eligible'],0)
 def test_two_provider_quorum(self):
  now=dt.datetime(2026,10,27,14,tzinfo=dt.timezone.utc)
  a=m.obs('A','MSFT',100,99.9,100.1,now.timestamp());b=m.obs('B','MSFT',100.2,100.1,100.3,now.timestamp())
  with patch.object(m,'PROVIDERS',[lambda t:a,lambda t:b]):c=m.consensus('MSFT',now)
  self.assertEqual(c['status'],'PASS');self.assertEqual(c['provider_count'],2);self.assertEqual(c['execution_eligible'],1)
 def test_outlier_rejected(self):
  now=dt.datetime(2026,10,27,14,tzinfo=dt.timezone.utc);xs=[m.obs('A','MSFT',100,stamp=now.timestamp()),m.obs('B','MSFT',100.1,stamp=now.timestamp()),m.obs('C','MSFT',80,stamp=now.timestamp())]
  with patch.object(m,'PROVIDERS',[lambda t,x=x:x for x in xs]):c=m.consensus('MSFT',now)
  self.assertEqual(c['status'],'PASS');self.assertNotIn('C',c['providers_used'])
 def test_one_source_valuation_only(self):
  now=dt.datetime(2026,10,27,14,tzinfo=dt.timezone.utc);x=m.obs('A','MSFT',100,stamp=now.timestamp())
  with patch.object(m,'PROVIDERS',[lambda t:x]):c=m.consensus('MSFT',now)
  self.assertEqual(c['status'],'VALUATION_ONLY');self.assertFalse(c['execution_eligible'])
 def test_identity_mismatch_rejected(self):
  x=m.validate(m.obs('A','MSFT',100,returned='AAPL'));self.assertFalse(x['accepted'])
 def test_readiness_all_positions(self):
  d=sqlite3.connect(':memory:');d.execute('create table quote_cache(ticker text primary key,price real,source text,quoted_at text,updated_epoch real)')
  now=dt.datetime(2026,10,27,14,tzinfo=dt.timezone.utc)
  def good(t,now=None):return {'ticker':t,'session':'REGULAR','status':'PASS','consensus_price':100,'providers_used':['A','B'],'provider_count':2,'max_deviation':.001,'execution_eligible':1,'observations':[]}
  with patch.object(m,'consensus',side_effect=good):r=m.refresh_all(d,['A','B'],now)
  self.assertEqual(r['status'],'PASS');self.assertEqual(r['covered'],2)
 def test_auth_failure_health(self):
  d=sqlite3.connect(':memory:');d.execute('create table quote_cache(ticker text primary key,price real,source text,quoted_at text,updated_epoch real)');m.init(d)
  c={'ticker':'X','session':'REGULAR','status':'FAILED','consensus_price':None,'providers_used':[],'provider_count':0,'max_deviation':None,'execution_eligible':0,'observations':[m.validate(m.obs('P','X',error='HTTPError',http=401))]};m.store(d,c)
  self.assertEqual(d.execute("select state from provider_health where provider='P'").fetchone()[0],'AUTH_FAILED')
 def test_v4_pauses_without_readiness(self):
  s=(Path(__file__).resolve().parents[1] / 'app' / 'v4_cycle.py').read_text(encoding='utf-8');self.assertIn('PAUSED_QUOTE_READINESS',s)
