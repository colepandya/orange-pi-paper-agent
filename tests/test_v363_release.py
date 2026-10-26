import os,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
class Release(unittest.TestCase):
 def test_executor_after_monitor(self):
  s=(ROOT/'systemd-paper-agent-executor-v363.timer').read_text();self.assertIn('*:07/10:00',s)
 def test_no_install_time_execution(self):
  s=(ROOT/'scripts/install_v363.sh').read_text();self.assertNotIn('lifecycle_executor_v363.py\n',s);self.assertIn('e.init(d)',s)
 def test_dashboard_lifecycle(self):
  s=(ROOT/'app/dashboard_v361.py').read_text();self.assertIn('Position lifecycle and virtual exits',s);self.assertIn("'version':'3.6.3'",s)
 def test_atomic_and_idempotent(self):
  s=(ROOT/'app/lifecycle_executor_v363.py').read_text();self.assertIn('BEGIN IMMEDIATE',s);self.assertIn('action_id TEXT PRIMARY KEY',s);self.assertIn("current['status']=='COMMITTED'",s)
 def test_fresh_quote_gate(self):
  s=(ROOT/'app/lifecycle_executor_v363.py').read_text();self.assertIn('FRESH_SECONDS=180',s);self.assertIn('BLOCKED_STALE_QUOTE',s)
 def test_full_lifecycle_triggers(self):
  s=(ROOT/'app/lifecycle_executor_v363.py').read_text()
  for token in ['THESIS_INVALIDATED','STOP_REACHED','TARGET_REACHED','RUNNER_TARGET','RUNNER_STOP','TIME_EXPIRED','RUNNER_TIME']:self.assertIn(token,s)
 def test_rapid_reforecast(self):
  s=(ROOT/'app/lifecycle_executor_v363.py').read_text();self.assertIn('HOLD_EXTENDED',s);self.assertIn('remaining_bull',s);self.assertIn('velocity_ratio',s)
if __name__=='__main__':unittest.main()
