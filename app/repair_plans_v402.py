#!/usr/bin/env python3
import json
import impact_v36 as impact,position_plans_v361 as plans,market_data_fabric_v402 as md
def run(d):
 tickers=[x[0] for x in d.execute("SELECT DISTINCT ticker FROM positions WHERE status='OPEN'")];ready=md.refresh_all(d,tickers)
 if ready['status']!='PASS':return {'status':'BLOCKED_QUOTE_READINESS',**{k:ready[k] for k in ('covered','execution_eligible','open_positions')}}
 d.execute("UPDATE position_forecast_plans SET active=0 WHERE active=1 AND plan_version IN ('3.6.1','3.6.2')");d.commit();created=plans.refresh(d);return {'status':'READY_FOR_MANUAL_REVIEW','plans_created':created}
if __name__=='__main__':d=impact.connect();print(json.dumps(run(d),indent=2,default=str));d.close()
