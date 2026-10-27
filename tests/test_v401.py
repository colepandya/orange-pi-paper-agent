import datetime as dt,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from app import market_clock_v401 as c
from app import quote_refresh_v401 as q
R=Path(__file__).resolve().parents[1]
class V401(unittest.TestCase):
 def test_sessions(self):
  z=dt.timezone.utc
  self.assertEqual(c.state(dt.datetime(2026,10,27,14,0,tzinfo=z))['session'],'REGULAR')
  self.assertEqual(c.state(dt.datetime(2026,10,27,22,0,tzinfo=z))['session'],'AFTER_HOURS')
  self.assertEqual(c.state(dt.datetime(2026,10,25,16,0,tzinfo=z))['session'],'CLOSED')
 def test_cadence(self):
  self.assertEqual(c.state(dt.datetime(2026,10,27,14,0,tzinfo=dt.timezone.utc))['refresh_seconds'],60)
  self.assertEqual(c.state(dt.datetime(2026,10,25,14,0,tzinfo=dt.timezone.utc))['refresh_seconds'],300)
 def test_regular_quote_live_eligible(self):
  now=dt.datetime(2026,10,27,14,0,tzinfo=dt.timezone.utc);payload={'quoteResponse':{'result':[{'symbol':'MSFT','regularMarketPrice':500.0,'regularMarketTime':int(now.timestamp()),'bid':499.9,'ask':500.1}]}}
  with patch('app.quote_refresh_v401.urllib.request.urlopen') as u:
   import io,json;u.return_value=io.BytesIO(json.dumps(payload).encode());x=q.fetch('MSFT',now)
  self.assertEqual(x['quality_state'],'LIVE');self.assertEqual(x['execution_eligible'],1)
 def test_identity_mismatch_blocks(self):
  now=dt.datetime(2026,10,27,14,0,tzinfo=dt.timezone.utc);payload={'quoteResponse':{'result':[{'symbol':'AAPL','regularMarketPrice':500.0,'regularMarketTime':int(now.timestamp())}]}}
  with patch('app.quote_refresh_v401.urllib.request.urlopen') as u:
   import io,json;u.return_value=io.BytesIO(json.dumps(payload).encode());x=q.fetch('MSFT',now)
  self.assertEqual(x['failure_reason'],'IDENTITY_MISMATCH');self.assertEqual(x['execution_eligible'],0)
 def test_after_hours_not_execution_eligible(self):
  now=dt.datetime(2026,10,27,22,0,tzinfo=dt.timezone.utc);payload={'quoteResponse':{'result':[{'symbol':'MSFT','postMarketPrice':510.0,'postMarketTime':int(now.timestamp())}]}}
  with patch('app.quote_refresh_v401.urllib.request.urlopen') as u:
   import io,json;u.return_value=io.BytesIO(json.dumps(payload).encode());x=q.fetch('MSFT',now)
  self.assertEqual(x['quality_state'],'EXTENDED_HOURS');self.assertEqual(x['execution_eligible'],0)
 def test_dashboard_read_only(self):
  for f in ['dashboard_v363.py','dashboard_v361.py','dashboard_single_v352.py']:
   p=R/'app'/f
   if p.exists():self.assertNotIn('quote_refresh_v401',p.read_text())
 def test_v4_forced_refresh_before_executor(self):
  s=(R/'app/v4_cycle.py').read_text();self.assertLess(s.index("result['quotes']"),s.index("result['executor']"));self.assertTrue('quote_refresh_v401' in s or 'market_data_fabric_v402' in s)
 def test_service_resources(self):
  s=(R/'systemd/paper-agent-quotes-v401.service').read_text();self.assertIn('CPUQuota=20%',s);self.assertIn('MemoryMax=100M',s)
