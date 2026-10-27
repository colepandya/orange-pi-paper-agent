import datetime as dt,os,tempfile,unittest
from pathlib import Path
from app import v4_safety as s
R=Path(__file__).resolve().parents[1]
class V4(unittest.TestCase):
 def test_boundaries(self):
  self.assertFalse(s.phrase('software update','war'));self.assertFalse(s.phrase('forward outlook','war'));self.assertFalse(s.phrase('bank results','ban'));self.assertTrue(s.phrase('war disrupts supply','war'))
 def test_negation(self):self.assertLess(s.direction('Acme did not win contract award'),0)
 def test_timestamp_fail_closed(self):
  n=dt.datetime(2026,1,2,tzinfo=dt.timezone.utc);self.assertFalse(s.freshness('garbage',n)[0]);self.assertFalse(s.freshness('2026-01-03T00:00:00+00:00',n)[0]);self.assertTrue(s.freshness('Thu, 01 Jan 2026 12:00:00 +0000',n)[0])
 def test_adverse_tightens_stop(self):self.assertGreater(s.tighten_stop(-.0415,-1),-.0415)
 def test_quote_identity_and_age(self):
  n=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc);q={'ticker':'AAPL','price':100,'quoted_at':n.isoformat()};self.assertEqual(s.quote_valid(q,'MSFT',n)[1],'IDENTITY_MISMATCH');self.assertTrue(s.quote_valid(q,'AAPL',n)[0]);self.assertFalse(s.quote_valid(q,'AAPL',n+dt.timedelta(seconds=181))[0])
 def test_forecast_reject_blocks(self):
  f={'decision':'REJECT','confidence':90,'data_completeness':90,'target_probability':.9,'target_return':.1,'allocation_pct':.2};self.assertEqual(s.forecast_gate(f)[0],'REJECT')
 def test_risk_limits(self):
  f={'decision':'BUY','confidence':90,'data_completeness':90,'target_probability':.9,'target_return':.1,'allocation_pct':.2};self.assertEqual(s.forecast_gate(f)[1],.05);self.assertIn('DRAWDOWN_PAUSE',s.forecast_gate(f,{'drawdown':-.06})[2])
 def test_no_unsafe_stooq(self):self.assertNotIn('Apple Inc', (R/'app/experiment_execution.py').read_text())
 def test_cash_read_inside_transactions(self):
  b=(R/'app/experiment_execution.py').read_text();self.assertLess(b.index("begin immediate"),b.index("cash=float",b.index("begin immediate")))
  e=(R/'app/lifecycle_executor_v363.py').read_text();self.assertLess(e.index("BEGIN IMMEDIATE"),e.index("cashrow=d.execute",e.index("BEGIN IMMEDIATE")))
 def test_single_authoritative_cycle(self):
  i=(R/'scripts/install_v4.sh').read_text();self.assertIn('paper-agent-v4.timer',i);self.assertIn('paper-agent-executor-v363.timer',i);self.assertIn('paper-agent-broad.timer',i)
 def test_forecast_gate_before_buy(self):
  b=(R/'app/broad_collector.py').read_text();self.assertLess(b.index('forecast_gate'),b.index('core.execute_virtual_buy',b.index('forecast_gate')))
 def test_pending_retry_disabled(self):self.assertIn('V4 expires legacy pending decisions',(R/'app/broad_collector.py').read_text())
 def test_quote_refresh_precedes_monitor_exit(self):
  x=(R/'app/v4_cycle.py').read_text();self.assertLess(x.index("result['quotes']"),x.index("result['monitor']"));self.assertLess(x.index("result['monitor']"),x.index("result['executor']"))
 def test_virtual_only(self):
  x=''.join((R/'app'/n).read_text().lower() for n in ['v4_cycle.py','v4_safety.py']);self.assertNotIn('broker',x);self.assertNotIn('submit_order',x)
if __name__=='__main__':unittest.main()
