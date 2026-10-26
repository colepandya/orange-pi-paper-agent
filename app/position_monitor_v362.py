#!/usr/bin/env python3
"""Reassess open-position plans after every collector cycle using newly ingested events."""
import datetime as dt,json,re
try:
 from . import impact_v36 as impact
 from . import position_plans_v361 as plans
except ImportError:
 import impact_v36 as impact, position_plans_v361 as plans
VERSION='3.6.2'
SCHEMA='''
CREATE TABLE IF NOT EXISTS position_monitor_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,created TEXT NOT NULL,status TEXT NOT NULL,open_positions INTEGER NOT NULL,events_reviewed INTEGER NOT NULL,plans_changed INTEGER NOT NULL,detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS position_event_links(position_id INTEGER NOT NULL,news_uid TEXT NOT NULL,linked_at TEXT NOT NULL,relevance REAL NOT NULL,direction REAL NOT NULL,scope TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(position_id,news_uid));
CREATE TABLE IF NOT EXISTS position_plan_changes(id INTEGER PRIMARY KEY AUTOINCREMENT,position_id INTEGER NOT NULL,created TEXT NOT NULL,prior_plan_id INTEGER,new_plan_id INTEGER,change_type TEXT NOT NULL,reason TEXT NOT NULL,event_uids_json TEXT NOT NULL);
'''
POS=('award','contract','approval','approved','partnership','beats','raises guidance','buyback','upgrade','expands','launch','record revenue','settlement')
NEG=('offering','dilution','recall','investigation','probe','lawsuit','breach','cyber','downgrade','cuts guidance','default','sanction','tariff','war','conflict','cancelled','termination')
MACRO=('tariff','sanction','interest rate','federal reserve','war','conflict','oil price','inflation','government shutdown','recession','export control','currency')
def now():return impact.now()
def parse(v):
 try:return dt.datetime.fromisoformat(str(v).replace('Z','+00:00'))
 except Exception:return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
def direction(text):
 t=text.lower();p=sum(x in t for x in POS);n=sum(x in t for x in NEG);return max(-1,min(1,(p-n)/max(1,p+n)))
def relevance(position,event):
 text=' '.join(str(event[k] or '') for k in ('title','summary')).lower();ticker=str(position['ticker']).lower();theme=str(position['theme'] or '').lower();tokens=[x for x in re.findall(r'[a-z]{4,}',theme) if x not in ('unclassified','and','the')]
 direct=1.0 if re.search(r'(?<![a-z0-9])'+re.escape(ticker)+r'(?![a-z0-9])',text) else 0
 thematic=min(.65,.18*sum(x in text for x in tokens));macro=.32 if any(x in text for x in MACRO) else 0
 score=max(direct,thematic,macro);scope='company' if direct else 'theme' if thematic>=macro and thematic else 'world' if macro else 'none';return score,scope
def monitor(d):
 impact.init(d);d.executescript(SCHEMA);
 missing=d.execute("SELECT COUNT(*) FROM positions p LEFT JOIN position_forecast_plans fp ON fp.position_id=p.id AND fp.active=1 WHERE p.status='OPEN' AND fp.id IS NULL").fetchone()[0] if d.execute("SELECT 1 FROM sqlite_master WHERE name='position_forecast_plans'").fetchone() else 1
 if missing:plans.refresh(d)
 positions=d.execute("SELECT * FROM positions WHERE status='OPEN'").fetchall();reviewed=changed=0;details=[]
 for p in positions:
  current=d.execute('SELECT * FROM position_forecast_plans WHERE position_id=? AND active=1 ORDER BY id DESC LIMIT 1',(p['id'],)).fetchone();since=current['created'] if current else p['opened'];events=d.execute('SELECT uid,source,title,summary,url,published,ingested FROM news WHERE COALESCE(ingested,published)>? ORDER BY COALESCE(ingested,published)',(since,)).fetchall();linked=[]
  for e in events:
   rel,scope=relevance(p,e)
   if rel<.30:continue
   dr=direction(' '.join(str(e[k] or '') for k in ('title','summary')));reason=f"{scope.title()} relevance {rel:.0%}; inferred event direction {dr:+.0%}; source {e['source']}"
   d.execute('INSERT OR IGNORE INTO position_event_links VALUES(?,?,?,?,?,?,?)',(p['id'],e['uid'],now(),rel,dr,scope,reason));linked.append((e,rel,dr,scope,reason));reviewed+=1
  if not linked:continue
  weighted=sum(rel*dr for _,rel,dr,_,_ in linked)/sum(rel for _,rel,_,_,_ in linked);prior_id=current['id'];prior_target=float(current['target_return']);prior_stop=float(current['stop_return']);prior_days=int(current['max_hold_days'])
  # bounded evidence update: ±35% target change, stop tightens on adverse news, hold changes by at most 2 sessions per cycle
  new_target=max(.005,min(.20,prior_target*(1+max(-.35,min(.35,weighted*.35)))))
  new_stop=prior_stop if weighted>=0 else min(-.012,prior_stop*(1-max(-.25,weighted*.20)))
  new_days=max(1,min(15,prior_days+(1 if weighted>.35 else -2 if weighted<-.35 else -1 if weighted<-.10 else 0)))
  material=abs(new_target-prior_target)>=.0025 or abs(new_stop-prior_stop)>=.0025 or new_days!=prior_days
  change='UNCHANGED'
  if material:
   change='EXPANDED' if weighted>.10 else 'TIGHTENED' if weighted<-.10 else 'REVISED';d.execute('UPDATE position_forecast_plans SET active=0 WHERE id=?',(prior_id,));entry=float(p['entry_price']);exit_at=plans.trading_exit(p['opened'],new_days);rationale=f"Live reassessment after {len(linked)} relevant new event(s). Net evidence direction {weighted:+.1%}. Previous target {prior_target:+.2%}, stop {prior_stop:+.2%}, hold {prior_days}; revised target {new_target:+.2%}, stop {new_stop:+.2%}, hold {new_days}."
   vals=(p['id'],p['decision_id'],now(),VERSION,1,current['event_type'],current['forecast_decision'],new_target,new_stop,entry*(1+new_target),entry*(1+new_stop),new_days,exit_at,current['target_probability'],current['confidence'],current['completeness'],current['partial_exit_pct'],current['runner_target_return'],entry*(1+float(current['runner_target_return'])),current['runner_stop_return'],entry*(1+float(current['runner_stop_return'])),current['bear_return'],current['base_return'],current['bull_return'],current['expected_return'],current['residual_return'],current['priced_in'],rationale,current['invalidation_json'],current['forecast_hash'])
   d.execute('INSERT INTO position_forecast_plans(position_id,decision_id,created,plan_version,active,event_type,forecast_decision,target_return,stop_return,target_price,stop_price,max_hold_days,estimated_exit,target_probability,confidence,completeness,partial_exit_pct,runner_target_return,runner_target_price,runner_stop_return,runner_stop_price,bear_return,base_return,bull_return,expected_return,residual_return,priced_in,rationale,invalidation_json,forecast_hash) VALUES('+','.join('?'*len(vals))+')',vals);newid=d.execute('SELECT last_insert_rowid()').fetchone()[0];d.execute('INSERT INTO position_plan_changes(position_id,created,prior_plan_id,new_plan_id,change_type,reason,event_uids_json) VALUES(?,?,?,?,?,?,?)',(p['id'],now(),prior_id,newid,change,rationale,json.dumps([x[0]['uid'] for x in linked])));changed+=1
  details.append({'ticker':p['ticker'],'events':len(linked),'direction':weighted,'change':change})
 d.execute('INSERT INTO position_monitor_runs(created,status,open_positions,events_reviewed,plans_changed,detail_json) VALUES(?,?,?,?,?,?)',(now(),'OK',len(positions),reviewed,changed,json.dumps(details,sort_keys=True)));d.commit();return {'version':VERSION,'open_positions':len(positions),'events_reviewed':reviewed,'plans_changed':changed,'details':details}
if __name__=='__main__':
 d=impact.connect();print(json.dumps(monitor(d),indent=2));d.close()
