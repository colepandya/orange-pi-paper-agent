#!/usr/bin/env python3
"""Versioned V3.6.1 forecast plans for open virtual positions."""
import datetime as dt,json,sqlite3
try:
 from . import impact_v36 as engine
except ImportError:
 import impact_v36 as engine
VERSION='3.6.1'
SCHEMA='''
CREATE TABLE IF NOT EXISTS position_forecast_plans(
 id INTEGER PRIMARY KEY AUTOINCREMENT,position_id INTEGER NOT NULL,decision_id INTEGER NOT NULL,created TEXT NOT NULL,
 plan_version TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,event_type TEXT NOT NULL,forecast_decision TEXT NOT NULL,
 target_return REAL NOT NULL,stop_return REAL NOT NULL,target_price REAL NOT NULL,stop_price REAL NOT NULL,
 max_hold_days INTEGER NOT NULL,estimated_exit TEXT NOT NULL,target_probability REAL NOT NULL,confidence REAL NOT NULL,
 completeness REAL NOT NULL,partial_exit_pct REAL NOT NULL,runner_target_return REAL NOT NULL,runner_target_price REAL NOT NULL,
 runner_stop_return REAL NOT NULL,runner_stop_price REAL NOT NULL,bear_return REAL NOT NULL,base_return REAL NOT NULL,
 bull_return REAL NOT NULL,expected_return REAL NOT NULL,residual_return REAL NOT NULL,priced_in REAL NOT NULL,
 rationale TEXT NOT NULL,invalidation_json TEXT NOT NULL,forecast_hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_position_plan_active ON position_forecast_plans(position_id,active,created DESC);
'''
def trading_exit(opened,days):
 x=dt.datetime.fromisoformat(str(opened).replace('Z','+00:00'))
 if x.tzinfo is None:x=x.replace(tzinfo=dt.timezone.utc)
 left=int(days)
 while left:
  x+=dt.timedelta(days=1)
  if x.weekday()<5:left-=1
 return x.replace(microsecond=0).isoformat()
def refresh(d):
 engine.init(d);d.executescript(SCHEMA)
 rows=d.execute('''SELECT p.id position_id,p.opened,p.entry_price,x.*,n.source,n.title,n.summary,n.url,n.published,t.event_type,t.thesis_json FROM positions p JOIN decisions x ON x.id=p.decision_id LEFT JOIN news n ON n.uid=x.news_uid LEFT JOIN theses t ON t.decision_id=x.id WHERE p.status='OPEN' ORDER BY p.id''').fetchall();out=[]
 for row in rows:
  payload=engine.forecast(d,row);event=payload[3];confidence=payload[6];complete=payload[7];bear,base,bull,expected=payload[11:15];target=float(payload[15]);stop=abs(float(payload[16]));days=int(payload[17]);prob=float(payload[18]);partial=float(payload[20]);runner=float(payload[21]);rstop=float(payload[22]);decision=payload[23];invalid=payload[24];deriv=json.loads(payload[26]);fhash=payload[27];entry=float(row['entry_price']);exit_at=trading_exit(row['opened'],days)
  rationale=(f"{event.replace('_',' ').title()} forecast. Estimated total impact {deriv['expected_total_impact']:+.2%}; "f"priced-in adjustment {deriv['priced_in']:+.2%}; residual opportunity {deriv['residual_return']:+.2%}. "f"Target is the residual base case after uncertainty and estimated execution costs. Stop reflects forecast downside, not the legacy fixed rule.")
  d.execute('UPDATE position_forecast_plans SET active=0 WHERE position_id=? AND active=1',(row['position_id'],))
  vals=(row['position_id'],row['id'],engine.now(),VERSION,1,event,decision,target,-stop,entry*(1+target),entry*(1-stop),days,exit_at,prob,confidence,complete,partial,runner,entry*(1+runner),rstop,entry*(1+rstop),bear,base,bull,expected,payload[10],payload[9],rationale,invalid,fhash)
  d.execute('INSERT INTO position_forecast_plans(position_id,decision_id,created,plan_version,active,event_type,forecast_decision,target_return,stop_return,target_price,stop_price,max_hold_days,estimated_exit,target_probability,confidence,completeness,partial_exit_pct,runner_target_return,runner_target_price,runner_stop_return,runner_stop_price,bear_return,base_return,bull_return,expected_return,residual_return,priced_in,rationale,invalidation_json,forecast_hash) VALUES('+','.join('?'*len(vals))+')',vals);out.append({'position_id':row['position_id'],'ticker':row['ticker'],'target':target,'stop':-stop,'days':days,'decision':decision})
 d.commit();return out
if __name__=='__main__':
 d=engine.connect();print(json.dumps({'version':VERSION,'plans':refresh(d)},indent=2));d.close()
