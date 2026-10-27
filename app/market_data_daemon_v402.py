#!/usr/bin/env python3
import json,time
import impact_v36 as impact,market_clock_v401 as clock,market_data_fabric_v402 as f
while True:
 d=impact.connect()
 try:r=f.refresh_all(d,[x[0] for x in d.execute("SELECT DISTINCT ticker FROM positions WHERE status IN ('OPEN','RUNNER_OPEN')")]);print(json.dumps({k:r[k] for k in ('status','open_positions','covered','execution_eligible')}),flush=True);wait=clock.state()['refresh_seconds']
 finally:d.close()
 time.sleep(wait)
