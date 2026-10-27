#!/usr/bin/env python3
"""One authoritative V4 cycle. Virtual-only."""
import fcntl,json,os,sys
import broad_collector,impact_v36,position_monitor_v362,lifecycle_executor_v363,market_data_fabric_v402
LOCK='/run/lock/paper-agent-v4.lock'
def cycle():
 os.environ['PAPER_AGENT_V4']='1';os.makedirs('/run/lock',exist_ok=True)
 with open(LOCK,'w') as f:
  fcntl.flock(f,fcntl.LOCK_EX);result={}
  result['collector']=broad_collector.cycle('/etc/paper-agent/broad-sources.json')
  d=impact_v36.connect();watch=json.load(open('/etc/paper-agent/watchlist.json'))
  result['quotes']=market_data_fabric_v402.refresh_all(d,[x[0] for x in d.execute("SELECT DISTINCT ticker FROM positions WHERE status IN ('OPEN','RUNNER_OPEN')")])
  if result['quotes']['status']!='PASS':
   result['monitor']={'status':'SKIPPED_QUOTE_READINESS'};result['executor']={'status':'PAUSED_QUOTE_READINESS'}
  else:
   result['monitor']=position_monitor_v362.monitor(d)
   result['executor']=lifecycle_executor_v363.run(d)
  result['integrity']=d.execute('PRAGMA integrity_check').fetchone()[0];d.close();return result
if __name__=='__main__':print(json.dumps(cycle(),indent=2,default=str))
