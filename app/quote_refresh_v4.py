#!/usr/bin/env python3
import json,os,time
import impact_v36 as impact,experiment_execution as q

def refresh(d,watch):
 out=[]
 tickers=[r[0] for r in d.execute("SELECT DISTINCT ticker FROM positions WHERE status='OPEN'")]
 for ticker in tickers:
  meta=watch.get(ticker)
  if not meta:out.append({'ticker':ticker,'status':'NO_SECURITY_MASTER'});continue
  x=q.resolve_quote(d,ticker,meta);x['ticker']=ticker
  out.append({'ticker':ticker,'status':'OK' if x.get('price') else 'FAILED','source':x.get('source'),'quoted_at':x.get('quoted_at')})
 return out
if __name__=='__main__':
 d=impact.connect();watch=json.load(open('/etc/paper-agent/watchlist.json'));print(json.dumps(refresh(d,watch),indent=2));d.close()
