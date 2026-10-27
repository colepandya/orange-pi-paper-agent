#!/usr/bin/env python3
"""Atomic, idempotent V3.6.3 virtual position lifecycle executor."""
import contextlib,datetime as dt,fcntl,hashlib,json,os,sqlite3,time
try:
 from . import impact_v36 as impact
 from . import position_plans_v361 as plans
except ImportError:
 import impact_v36 as impact, position_plans_v361 as plans
VERSION='3.6.3';DB='/var/lib/paper-agent/paper-agent.db';LOCK='/run/lock/paper-agent-v363.lock';FRESH_SECONDS=180
SCHEMA='''
CREATE TABLE IF NOT EXISTS position_lifecycle(
 position_id INTEGER PRIMARY KEY,phase TEXT NOT NULL DEFAULT 'OPEN',remaining_shares REAL NOT NULL,remaining_cost REAL NOT NULL,
 realized_pnl REAL NOT NULL DEFAULT 0,last_action_at TEXT,last_action_id TEXT,runner_plan_id INTEGER,updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS virtual_exit_actions(
 action_id TEXT PRIMARY KEY,position_id INTEGER NOT NULL,decision_id INTEGER NOT NULL,plan_id INTEGER NOT NULL,created TEXT NOT NULL,
 status TEXT NOT NULL,trigger TEXT NOT NULL,phase_before TEXT NOT NULL,phase_after TEXT,shares_requested REAL NOT NULL,
 shares_filled REAL,observed_price REAL,fill_price REAL,slippage_bps REAL,quote_source TEXT,quote_time TEXT,quote_age_seconds REAL,
 proceeds REAL,cost_released REAL,realized_pnl REAL,prior_cash REAL,new_cash REAL,reason TEXT NOT NULL,plan_json TEXT NOT NULL,
 evidence_json TEXT NOT NULL,committed_at TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS target_reforecasts(
 id INTEGER PRIMARY KEY AUTOINCREMENT,position_id INTEGER NOT NULL,prior_plan_id INTEGER NOT NULL,new_plan_id INTEGER,
 created TEXT NOT NULL,decision TEXT NOT NULL,observed_return REAL NOT NULL,elapsed_sessions INTEGER NOT NULL,
 velocity_ratio REAL NOT NULL,remaining_bull REAL NOT NULL,confidence REAL NOT NULL,reason TEXT NOT NULL,
 UNIQUE(position_id,prior_plan_id));
CREATE TABLE IF NOT EXISTS executor_runs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,created TEXT NOT NULL,status TEXT NOT NULL,positions_checked INTEGER NOT NULL,
 actions_committed INTEGER NOT NULL,holds_extended INTEGER NOT NULL,blocked_stale INTEGER NOT NULL,errors INTEGER NOT NULL,
 detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v363_price_ranges(
 position_id INTEGER NOT NULL,plan_id INTEGER NOT NULL,observed_at TEXT NOT NULL,low REAL NOT NULL,high REAL NOT NULL,
 source TEXT NOT NULL,sequence_known INTEGER NOT NULL DEFAULT 0,first_trigger TEXT,PRIMARY KEY(position_id,plan_id,observed_at));
CREATE INDEX IF NOT EXISTS idx_exit_position ON virtual_exit_actions(position_id,created);
CREATE INDEX IF NOT EXISTS idx_lifecycle_phase ON position_lifecycle(phase);
'''
def now():return impact.now()
def parse(v):
 try:
  x=dt.datetime.fromisoformat(str(v).replace('Z','+00:00'));return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)
 except Exception:return None
def age_seconds(v,asof=None):
 x=parse(v);return 10**12 if x is None else max(0,((asof or dt.datetime.now(dt.timezone.utc))-x).total_seconds())
def action_id(position,plan,trigger,phase,shares):return hashlib.sha256(f'{position}:{plan}:{trigger}:{phase}:{float(shares):.8f}'.encode()).hexdigest()
@contextlib.contextmanager
def writer_lock(path=LOCK):
 os.makedirs(os.path.dirname(path),exist_ok=True);f=open(path,'w');fcntl.flock(f,fcntl.LOCK_EX)
 try:yield
 finally:fcntl.flock(f,fcntl.LOCK_UN);f.close()
def init(d):
 d.executescript(SCHEMA)
 rows=d.execute("SELECT id,shares,cost,status FROM positions WHERE status='OPEN'").fetchall()
 for r in rows:d.execute('INSERT OR IGNORE INTO position_lifecycle(position_id,phase,remaining_shares,remaining_cost,updated) VALUES(?,?,?,?,?)',(r['id'],'OPEN',r['shares'],r['cost'],now()))
 d.commit()
def cached_quote(d,ticker,asof=None):
 if not d.execute("SELECT 1 FROM sqlite_master WHERE name='quote_cache'").fetchone():return None
 r=d.execute('SELECT price,source,quoted_at FROM quote_cache WHERE ticker=?',(ticker,)).fetchone()
 if not r:return None
 return {'price':float(r['price']),'source':r['source'],'quoted_at':r['quoted_at'],'age':age_seconds(r['quoted_at'],asof)}
def trading_sessions(opened,asof):
 a=parse(opened);b=asof if isinstance(asof,dt.datetime) else parse(asof);n=0
 if not a or not b:return 0
 x=a.date()
 while x<b.date():
  x+=dt.timedelta(days=1)
  if x.weekday()<5:n+=1
 return n
def latest_adverse(d,position_id,after):
 if not d.execute("SELECT 1 FROM sqlite_master WHERE name='position_event_links'").fetchone():return None
 return d.execute('SELECT * FROM position_event_links WHERE position_id=? AND linked_at>=? AND direction<=-0.75 ORDER BY linked_at DESC LIMIT 1',(position_id,after)).fetchone()
def choose_trigger(d,p,lifecycle,plan,q,asof):
 price=q['price'];phase=lifecycle['phase'];entry=float(p['entry_price']);held=trading_sessions(p['opened'],asof)
 adverse=latest_adverse(d,p['id'],lifecycle['last_action_at'] or p['opened'])
 if adverse:return 'THESIS_INVALIDATED',f"Adverse {adverse['scope']} event: {adverse['reason']}",held
 # If a captured range crossed both boundaries and order is unknown, use conservative stop-first handling.
 rng=d.execute('SELECT * FROM v363_price_ranges WHERE position_id=? AND plan_id=? ORDER BY observed_at DESC LIMIT 1',(p['id'],plan['id'])).fetchone()
 if rng and float(rng['low'])<=float(plan['stop_price']) and float(rng['high'])>=float(plan['target_price']):
  first=rng['first_trigger'] if rng['sequence_known'] else 'STOP'
  return ('STOP_REACHED' if first=='STOP' else 'TARGET_REACHED'),'Both target and stop crossed; '+('observed ordering used' if rng['sequence_known'] else 'unknown ordering, conservative stop-first policy'),held
 if price<=float(plan['stop_price']):return ('RUNNER_STOP' if phase=='RUNNER_OPEN' else 'STOP_REACHED'),f"Fresh price {price:.4f} reached stop {float(plan['stop_price']):.4f}",held
 if price>=float(plan['target_price']):return ('RUNNER_TARGET' if phase=='RUNNER_OPEN' else 'TARGET_REACHED'),f"Fresh price {price:.4f} reached target {float(plan['target_price']):.4f}",held
 if held>=int(plan['max_hold_days']):return ('RUNNER_TIME' if phase=='RUNNER_OPEN' else 'TIME_EXPIRED'),f"Held {held} sessions versus maximum {plan['max_hold_days']}",held
 return None,'No trigger',held
def rapid_reforecast(d,p,lifecycle,plan,q,held):
 if lifecycle['phase']!='OPEN' or held>=max(1,int(plan['max_hold_days'])//2+1):return None
 prior=d.execute('SELECT * FROM target_reforecasts WHERE position_id=? AND prior_plan_id=?',(p['id'],plan['id'])).fetchone()
 if prior:return None
 observed=q['price']/float(p['entry_price'])-1;target=max(.001,float(plan['target_return']));velocity=observed/target;remaining_bull=float(plan['bull_return'])-observed;confidence=float(plan['confidence'])
 adverse=latest_adverse(d,p['id'],lifecycle['last_action_at'] or p['opened'])
 hold=velocity>=1 and remaining_bull>=max(.012,target*.50) and confidence>=65 and adverse is None
 reason=(f"Target reached in {held} session(s), {velocity:.2f}x planned velocity. "f"Bull-case residual {remaining_bull:+.2%}; confidence {confidence:.0f}%. "f"Decision: {'extend and hold' if hold else 'follow exit plan'}.")
 newid=None
 if hold:
  extension=min(.20,max(observed+.01,float(plan['runner_target_return']),float(plan['bull_return'])*.90));days=min(15,max(held+1,int(plan['max_hold_days'])+1));entry=float(p['entry_price'])
  d.execute('UPDATE position_forecast_plans SET active=0 WHERE id=?',(plan['id'],))
  cols=[x[1] for x in d.execute('PRAGMA table_info(position_forecast_plans)')];values=dict(plan);values.update(created=now(),plan_version=VERSION,active=1,target_return=extension,target_price=entry*(1+extension),max_hold_days=days,estimated_exit=plans.trading_exit(p['opened'],days),rationale=reason)
  insert=[c for c in cols if c!='id'];d.execute('INSERT INTO position_forecast_plans('+','.join(insert)+') VALUES('+','.join('?'*len(insert))+')',[values[c] for c in insert]);newid=d.execute('SELECT last_insert_rowid()').fetchone()[0]
 d.execute('INSERT INTO target_reforecasts(position_id,prior_plan_id,new_plan_id,created,decision,observed_return,elapsed_sessions,velocity_ratio,remaining_bull,confidence,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(p['id'],plan['id'],newid,now(),'HOLD_EXTENDED' if hold else 'EXIT_PLAN',observed,held,velocity,remaining_bull,confidence,reason));d.commit()
 return {'hold':hold,'new_plan_id':newid,'reason':reason}

def create_runner_plan(d,p,plan):
 entry=float(p['entry_price']);target=float(plan['runner_target_return']);stop=float(plan['runner_stop_return']);days=min(15,max(1,int(plan['max_hold_days'])+2));cols=[x[1] for x in d.execute('PRAGMA table_info(position_forecast_plans)')];values=dict(plan);values.update(created=now(),plan_version=VERSION,active=1,target_return=target,stop_return=stop,target_price=entry*(1+target),stop_price=entry*(1+stop),max_hold_days=days,estimated_exit=plans.trading_exit(p['opened'],days),partial_exit_pct=1.0,rationale=f"Runner plan after partial target exit. Target {target:+.2%}; protected stop {stop:+.2%}; maximum hold {days} sessions.")
 d.execute('UPDATE position_forecast_plans SET active=0 WHERE id=?',(plan['id'],));fields=[c for c in cols if c!='id'];d.execute('INSERT INTO position_forecast_plans('+','.join(fields)+') VALUES('+','.join('?'*len(fields))+')',[values[c] for c in fields]);return d.execute('SELECT last_insert_rowid()').fetchone()[0]

def simulate_fill(trigger,observed,slippage_bps=8.0):
 adverse=-1 if trigger in ('TARGET_REACHED','RUNNER_TARGET','TIME_EXPIRED','RUNNER_TIME') else -1
 return max(.0001,float(observed)*(1+adverse*slippage_bps/10000))
def reserve_action(d,p,lifecycle,plan,trigger,reason,q,shares,evidence):
 aid=action_id(p['id'],plan['id'],trigger,lifecycle['phase'],shares);existing=d.execute('SELECT * FROM virtual_exit_actions WHERE action_id=?',(aid,)).fetchone()
 if existing:return aid,False
 d.execute('INSERT INTO virtual_exit_actions(action_id,position_id,decision_id,plan_id,created,status,trigger,phase_before,shares_requested,observed_price,quote_source,quote_time,quote_age_seconds,reason,plan_json,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(aid,p['id'],p['decision_id'],plan['id'],now(),'RESERVED',trigger,lifecycle['phase'],shares,q['price'],q['source'],q['quoted_at'],q['age'],reason,json.dumps(dict(plan),sort_keys=True,default=str),json.dumps(evidence,sort_keys=True,default=str)));d.commit();return aid,True
def commit_action(d,p,lifecycle,plan,trigger,reason,q,shares,aid):
 current=d.execute('SELECT * FROM virtual_exit_actions WHERE action_id=?',(aid,)).fetchone()
 if current['status']=='COMMITTED':return dict(current)
 fill=simulate_fill(trigger,q['price']);remaining_shares=float(lifecycle['remaining_shares']);remaining_cost=float(lifecycle['remaining_cost']);shares=min(float(shares),remaining_shares);fraction=shares/remaining_shares;released=round(remaining_cost*fraction,2);proceeds=round(fill*shares,2);pnl=round(proceeds-released,2)
 new_cash=None;left_shares=round(remaining_shares-shares,8);left_cost=round(remaining_cost-released,2);partial=left_shares>1e-8;phase_after='RUNNER_OPEN' if partial else 'CLOSED';kind='VIRTUAL_PARTIAL_SELL' if partial else 'VIRTUAL_SELL_V363'
 d.execute('BEGIN IMMEDIATE')
 cashrow=d.execute('SELECT balance FROM ledger ORDER BY id DESC LIMIT 1').fetchone();prior_cash=float(cashrow[0]) if cashrow else 0;new_cash=round(prior_cash+proceeds,2)
 row=d.execute('SELECT status FROM virtual_exit_actions WHERE action_id=?',(aid,)).fetchone()
 if row['status']=='COMMITTED':d.rollback();return dict(d.execute('SELECT * FROM virtual_exit_actions WHERE action_id=?',(aid,)).fetchone())
 d.execute('INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)',(now(),kind,proceeds,f"{p['ticker']}:{p['id']}:{aid[:16]}",new_cash))
 total_realized=round(float(lifecycle['realized_pnl'])+pnl,2)
 if partial:
  runner_plan_id=create_runner_plan(d,p,plan)
  d.execute("UPDATE positions SET shares=?,cost=?,review_code=? WHERE id=?",(left_shares,left_cost,'RUNNER_OPEN',p['id']))
  d.execute('UPDATE position_lifecycle SET phase=?,remaining_shares=?,remaining_cost=?,realized_pnl=?,last_action_at=?,last_action_id=?,runner_plan_id=?,updated=? WHERE position_id=?',('RUNNER_OPEN',left_shares,left_cost,total_realized,now(),aid,runner_plan_id,now(),p['id']))
 else:
  d.execute("UPDATE positions SET shares=0,cost=0,status='CLOSED',closed=?,exit_price=?,pnl=?,review_code=? WHERE id=?",(now(),fill,total_realized,trigger,p['id']))
  d.execute('UPDATE position_forecast_plans SET active=0 WHERE position_id=?',(p['id'],))
  d.execute('UPDATE position_lifecycle SET phase=?,remaining_shares=0,remaining_cost=0,realized_pnl=?,last_action_at=?,last_action_id=?,updated=? WHERE position_id=?',('CLOSED',total_realized,now(),aid,now(),p['id']))
 d.execute('UPDATE virtual_exit_actions SET status=?,phase_after=?,shares_filled=?,fill_price=?,slippage_bps=?,proceeds=?,cost_released=?,realized_pnl=?,prior_cash=?,new_cash=?,committed_at=? WHERE action_id=?',('COMMITTED',phase_after,shares,fill,8.0,proceeds,released,pnl,prior_cash,new_cash,now(),aid));d.commit();return dict(d.execute('SELECT * FROM virtual_exit_actions WHERE action_id=?',(aid,)).fetchone())
def recover(d):
 # Reservations are safe to retry because no ledger mutation occurs before the atomic commit transaction.
 return d.execute("SELECT COUNT(*) FROM virtual_exit_actions WHERE status='RESERVED'").fetchone()[0]
def run(d,quote_provider=None,asof=None):
 init(d);asof=asof or dt.datetime.now(dt.timezone.utc);checked=committed=extended=blocked=errors=0;details=[]
 rows=d.execute("SELECT p.*,l.phase,l.remaining_shares,l.remaining_cost,l.realized_pnl,l.last_action_at,l.updated lifecycle_updated FROM positions p JOIN position_lifecycle l ON l.position_id=p.id WHERE p.status='OPEN'").fetchall()
 for p in rows:
  checked+=1
  try:
   plan=d.execute('SELECT * FROM position_forecast_plans WHERE position_id=? AND active=1 ORDER BY id DESC LIMIT 1',(p['id'],)).fetchone()
   if not plan:details.append({'ticker':p['ticker'],'result':'NO_ACTIVE_PLAN'});continue
   q=quote_provider(d,p,asof) if quote_provider else cached_quote(d,p['ticker'],asof)
   if not q or q.get('age',10**12)>FRESH_SECONDS:blocked+=1;details.append({'ticker':p['ticker'],'result':'BLOCKED_STALE_QUOTE'});continue
   trigger,reason,held=choose_trigger(d,p,p,plan,q,asof)
   if not trigger:details.append({'ticker':p['ticker'],'result':'HOLD'});continue
   if trigger=='TARGET_REACHED':
    rf=rapid_reforecast(d,p,p,plan,q,held)
    if rf and rf['hold']:extended+=1;details.append({'ticker':p['ticker'],'result':'HOLD_EXTENDED','reason':rf['reason']});continue
   fraction=float(plan['partial_exit_pct']) if trigger=='TARGET_REACHED' else 1.0;shares=float(p['remaining_shares'])*fraction
   evidence={'held_sessions':held,'trigger':trigger,'quote':q,'rapid_reforecast':d.execute('SELECT decision,reason FROM target_reforecasts WHERE position_id=? AND prior_plan_id=?',(p['id'],plan['id'])).fetchone()}
   aid,new=reserve_action(d,p,p,plan,trigger,reason,q,shares,evidence);action=commit_action(d,p,p,plan,trigger,reason,q,shares,aid);committed+=1 if new else 0;details.append({'ticker':p['ticker'],'result':action['phase_after'],'trigger':trigger,'pnl':action['realized_pnl']})
  except Exception as e:errors+=1;d.rollback();details.append({'ticker':p['ticker'],'result':'ERROR','error':type(e).__name__+': '+str(e)})
 d.execute('INSERT INTO executor_runs(created,status,positions_checked,actions_committed,holds_extended,blocked_stale,errors,detail_json) VALUES(?,?,?,?,?,?,?,?)',(now(),'OK' if not errors else 'ERROR',checked,committed,extended,blocked,errors,json.dumps(details,sort_keys=True,default=str)));d.commit();return {'version':VERSION,'positions_checked':checked,'actions_committed':committed,'holds_extended':extended,'blocked_stale':blocked,'errors':errors,'details':details,'pending_reservations':recover(d)}
if __name__=='__main__':
 with writer_lock():
  d=impact.connect();print(json.dumps(run(d),indent=2));d.close()
