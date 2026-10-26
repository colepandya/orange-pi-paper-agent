#!/usr/bin/env python3
"""V3.4 local measurement, reconciliation, virtual exits and governed research."""
import contextlib,datetime as dt,fcntl,hashlib,json,math,os,shutil,sqlite3,statistics,time
VERSION='3.4.0';DB='/var/lib/paper-agent/paper-agent.db';LOCK='/run/lock/paper-agent-writer.lock';WINDOWS=(1,3,5,10,15)
SCHEMA='''
CREATE TABLE IF NOT EXISTS v34_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_outcomes(decision_id INTEGER NOT NULL,window_days INTEGER NOT NULL,observed_at TEXT NOT NULL,eligible_date TEXT,stock_price REAL,spy_price REAL,sector_symbol TEXT,sector_price REAL,stock_return REAL,spy_return REAL,sector_return REAL,spy_abnormal REAL,sector_abnormal REAL,mfe REAL,mae REAL,target_hit INTEGER NOT NULL DEFAULT 0,stop_hit INTEGER NOT NULL DEFAULT 0,first_trigger TEXT,net_return REAL,PRIMARY KEY(decision_id,window_days));
CREATE TABLE IF NOT EXISTS virtual_exit_events(position_id INTEGER PRIMARY KEY,decision_id INTEGER NOT NULL,trigger TEXT NOT NULL,triggered_at TEXT NOT NULL,quote_price REAL NOT NULL,quote_source TEXT NOT NULL,quote_time TEXT NOT NULL,realized_pnl REAL NOT NULL,holding_days INTEGER NOT NULL,model_version TEXT NOT NULL,rule_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS daily_reconciliation(created TEXT PRIMARY KEY,integrity TEXT NOT NULL,cash REAL,ledger_balance REAL,open_cost REAL,orphan_positions INTEGER,missing_positions INTEGER,negative_cash INTEGER,stale_quotes INTEGER,critical INTEGER NOT NULL,detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS challenger_results(run_id TEXT NOT NULL,candidate_id TEXT NOT NULL,created TEXT NOT NULL,parameters_json TEXT NOT NULL,sample_size INTEGER NOT NULL,mean_abnormal REAL,median_abnormal REAL,win_rate REAL,max_drawdown REAL,profit_factor REAL,ci95 REAL,passes INTEGER NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(run_id,candidate_id));
CREATE TABLE IF NOT EXISTS strategy_recommendations(id TEXT PRIMARY KEY,created TEXT NOT NULL,status TEXT NOT NULL,champion_version TEXT NOT NULL,candidate_id TEXT NOT NULL,report_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_status_created ON decisions(status,created DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_ticker_created ON decisions(ticker,created DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_score ON decisions(score);
CREATE INDEX IF NOT EXISTS idx_theses_event ON theses(event_type);
'''
def now():return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
def connect(path=DB):
 d=sqlite3.connect(path,timeout=60);d.row_factory=sqlite3.Row;d.execute('PRAGMA busy_timeout=60000');d.execute('PRAGMA foreign_keys=ON');return d
def init(d):d.executescript(SCHEMA);d.execute('INSERT OR REPLACE INTO v34_meta VALUES(?,?)',('version',VERSION));d.commit()
@contextlib.contextmanager
def writer_lock(path=LOCK):
 os.makedirs(os.path.dirname(path),exist_ok=True);f=open(path,'w');fcntl.flock(f,fcntl.LOCK_EX);yield;fcntl.flock(f,fcntl.LOCK_UN);f.close()
def parse(v):
 try:
  x=dt.datetime.fromisoformat(str(v).replace('Z','+00:00'));return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)
 except Exception:return None
def trading_days(a,b):
 if not a or not b:return 0
 n=0;x=a.date()
 while x<b.date():x+=dt.timedelta(days=1);n+=x.weekday()<5
 return n
def reconcile(d,stale_seconds=86400):
 init(d);cashrow=d.execute('SELECT balance FROM ledger ORDER BY id DESC LIMIT 1').fetchone();cash=float(cashrow[0]) if cashrow else 0;ledger=cash;open_cost=float(d.execute("SELECT COALESCE(SUM(cost),0) FROM positions WHERE status='OPEN'").fetchone()[0]);orph=d.execute("SELECT COUNT(*) FROM positions p LEFT JOIN decisions x ON x.id=p.decision_id WHERE x.id IS NULL").fetchone()[0];miss=d.execute("SELECT COUNT(*) FROM decisions x LEFT JOIN positions p ON p.decision_id=x.id WHERE x.status='EXECUTED' AND p.id IS NULL").fetchone()[0];stale=d.execute("SELECT COUNT(*) FROM quote_cache WHERE updated_epoch<?",(time.time()-stale_seconds,)).fetchone()[0] if d.execute("SELECT 1 FROM sqlite_master WHERE name='quote_cache'").fetchone() else 0;integrity=d.execute('PRAGMA quick_check').fetchone()[0];critical=int(integrity!='ok' or cash<-.01 or orph or miss);detail={'equity_at_cost':cash+open_cost,'version':VERSION};d.execute('INSERT OR REPLACE INTO daily_reconciliation VALUES(?,?,?,?,?,?,?,?,?,?,?)',(now(),integrity,cash,ledger,open_cost,orph,miss,int(cash<-.01),stale,critical,json.dumps(detail,sort_keys=True)));d.commit();return {'integrity':integrity,'cash':cash,'open_cost':open_cost,'orphans':orph,'missing_positions':miss,'stale_quotes':stale,'critical':critical}
def exit_rule(d,event):
 r=d.execute('SELECT max_days,profit_target,stop_loss FROM event_exit_rules WHERE event_type=?',(event,)).fetchone();return tuple(r) if r else (10,.15,.10)
def evaluate_exits(d,quotes,asof=None):
 init(d);asof=asof or dt.datetime.now(dt.timezone.utc);out=[]
 rows=d.execute("SELECT p.*,t.event_type FROM positions p LEFT JOIN theses t ON t.decision_id=p.decision_id WHERE p.status='OPEN'").fetchall()
 for r in rows:
  q=quotes.get(r['ticker']);
  if not q or not q.get('price'):continue
  days,target,stop=exit_rule(d,r['event_type'] or 'general_news');held=trading_days(parse(r['opened']),asof);price=float(q['price']);entry=float(r['entry_price']);trigger='TARGET' if price>=entry*(1+target) else 'STOP' if price<=entry*(1-stop) else 'TIME' if held>=days else None
  if not trigger:continue
  pnl=round((price-entry)*float(r['shares']),2);rule=json.dumps({'max_days':days,'target':target,'stop':stop},sort_keys=True);d.execute('BEGIN IMMEDIATE')
  try:
   d.execute("UPDATE positions SET status='CLOSED',closed=?,exit_price=?,pnl=?,review_code=? WHERE id=?",(asof.isoformat(),price,pnl,trigger,r['id']));cash=float(d.execute('SELECT balance FROM ledger ORDER BY id DESC LIMIT 1').fetchone()[0]);d.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,'VIRTUAL_SELL',?,?,?)",(asof.isoformat(),round(price*float(r['shares']),2),f"{r['ticker']}:{r['decision_id']}:{trigger}",round(cash+price*float(r['shares']),2)));d.execute("INSERT INTO virtual_exit_events VALUES(?,?,?,?,?,?,?,?,?,?,?)",(r['id'],r['decision_id'],trigger,asof.isoformat(),price,q.get('source','UNKNOWN'),q.get('quoted_at',asof.isoformat()),pnl,held,VERSION,rule));d.commit();out.append({'ticker':r['ticker'],'trigger':trigger,'pnl':pnl})
  except Exception:d.rollback();raise
 return out
def max_drawdown(xs):
 peak=1;value=1;worst=0
 for x in xs:value*=1+x;peak=max(peak,value);worst=min(worst,value/peak-1)
 return worst
def metrics(values):
 n=len(values)
 if not n:return {'n':0,'mean':None,'median':None,'win_rate':None,'drawdown':None,'profit_factor':None,'ci95':None}
 gains=sum(x for x in values if x>0);loss=-sum(x for x in values if x<0);return {'n':n,'mean':statistics.mean(values),'median':statistics.median(values),'win_rate':sum(x>0 for x in values)/n,'drawdown':max_drawdown(values),'profit_factor':gains/loss if loss else None,'ci95':1.96*statistics.stdev(values)/math.sqrt(n) if n>1 else None}
def challenger_run(d,min_sample=75):
 init(d);rows=d.execute("SELECT x.score,o.sector_abnormal FROM decisions x JOIN decision_outcomes o ON o.decision_id=x.id AND o.window_days=10 WHERE o.sector_abnormal IS NOT NULL").fetchall();run=hashlib.sha256((now()+str(len(rows))).encode()).hexdigest()[:16];candidates=[('score65',65),('score70',70),('score75',75)]
 reports=[]
 for cid,threshold in candidates:
  vals=[float(x['sector_abnormal']) for x in rows if float(x['score'] or 0)>=threshold];m=metrics(vals);passes=int(m['n']>=min_sample and (m['mean'] or 0)>0 and (m['drawdown'] or -1)>=-.25 and (m['profit_factor'] or 0)>=1.2);reason='PASS' if passes else 'insufficient sample or risk-adjusted evidence';params=json.dumps({'entry_score':threshold},sort_keys=True);d.execute('INSERT INTO challenger_results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(run,cid,now(),params,m['n'],m['mean'],m['median'],m['win_rate'],m['drawdown'],m['profit_factor'],m['ci95'],passes,reason));reports.append({'candidate':cid,**m,'passes':bool(passes)})
 winner=next((x for x in reports if x['passes'] and x['candidate']!='score65'),None)
 if winner:
  rid=hashlib.sha256((run+winner['candidate']).encode()).hexdigest();d.execute('INSERT INTO strategy_recommendations VALUES(?,?,?,?,?,?)',(rid,now(),'PENDING_APPROVAL','3.4.0-champion',winner['candidate'],json.dumps(winner,sort_keys=True)))
 d.commit();return {'run_id':run,'candidates':reports,'recommendation':winner}
