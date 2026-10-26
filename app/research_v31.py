#!/usr/bin/env python3
"""V3.1 research and measurement layer. Virtual research only."""
import datetime as dt, json, math, sqlite3, statistics, urllib.parse, urllib.request
VERSION='3.1.0'; WINDOWS=(1,3,5,10,15)
SCHEMA='''
CREATE TABLE IF NOT EXISTS activation_state(id INTEGER PRIMARY KEY CHECK(id=1),activated_at TEXT NOT NULL,baseline_complete INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS company_fundamentals(ticker TEXT PRIMARY KEY,cik TEXT,revenue REAL,operating_income REAL,cash REAL,debt REAL,shares REAL,market_cap REAL,backlog REAL,as_of TEXT,source_url TEXT);
CREATE TABLE IF NOT EXISTS company_relationships(entity_name TEXT NOT NULL,parent_ticker TEXT NOT NULL,relationship TEXT NOT NULL,valid_from TEXT,valid_to TEXT,evidence_url TEXT NOT NULL,confidence REAL NOT NULL,PRIMARY KEY(entity_name,parent_ticker,relationship));
CREATE TABLE IF NOT EXISTS regime_snapshots(observed_at TEXT PRIMARY KEY,spy_price REAL,spy_20d_return REAL,spy_60d_return REAL,volatility_20d REAL,regime TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS strategy_trials(trial_id TEXT PRIMARY KEY,model_version TEXT NOT NULL,started TEXT NOT NULL,ended TEXT,status TEXT NOT NULL,parameters_json TEXT NOT NULL,notes TEXT);
CREATE TABLE IF NOT EXISTS event_exit_rules(event_type TEXT PRIMARY KEY,max_days INTEGER NOT NULL,profit_target REAL NOT NULL,stop_loss REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outcome_observations(decision_id INTEGER NOT NULL,window_days INTEGER NOT NULL,observed_at TEXT NOT NULL,stock_price REAL,benchmark_symbol TEXT,benchmark_price REAL,stock_return REAL,benchmark_return REAL,abnormal_return REAL,mfe REAL,mae REAL,PRIMARY KEY(decision_id,window_days));
CREATE TABLE IF NOT EXISTS research_reports(created TEXT NOT NULL,model_version TEXT NOT NULL,report_json TEXT NOT NULL);
'''
EXIT_RULES={'government_contract':(10,.15,.10),'fda_regulatory':(5,.20,.12),'earnings_guidance':(10,.15,.10),'insider_ownership':(15,.20,.12),'merger_transaction':(15,.10,.08),'capital_dilution':(5,.12,.10),'product_customer':(10,.15,.10),'cyber_incident':(5,.12,.10),'regulation_policy':(10,.15,.10),'general_news':(10,.15,.10)}
def now():return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
def init(db,activated_at=None):
 db.executescript(SCHEMA); activated_at=activated_at or now();db.execute('INSERT OR IGNORE INTO activation_state VALUES(1,?,0)',(activated_at,))
 for k,v in EXIT_RULES.items():db.execute('INSERT OR REPLACE INTO event_exit_rules VALUES(?,?,?,?)',(k,*v))
 db.commit()
def baseline_only(db,published):
 init(db);row=db.execute('SELECT activated_at,baseline_complete FROM activation_state WHERE id=1').fetchone();activation=dt.datetime.fromisoformat(row[0]);
 try:event=dt.datetime.fromisoformat(str(published).replace('Z','+00:00'))
 except Exception:return not bool(row[1])
 if event.tzinfo is None:event=event.replace(tzinfo=dt.timezone.utc)
 return event<activation
def mark_baseline_complete(db):db.execute('UPDATE activation_state SET baseline_complete=1 WHERE id=1');db.commit()
def materiality(db,ticker,amount):
 row=db.execute('SELECT revenue,market_cap,backlog FROM company_fundamentals WHERE ticker=?',(ticker,)).fetchone();amount=float(amount or 0)
 if not row:return {'amount':amount,'revenue_ratio':None,'market_cap_ratio':None,'backlog_ratio':None,'score':None}
 ratios=[amount/float(x) if x else None for x in row];valid=[x for x in ratios if x is not None];ratio=max(valid) if valid else 0
 score=20 if ratio>=.10 else 17 if ratio>=.05 else 14 if ratio>=.02 else 10 if ratio>=.005 else 4
 return {'amount':amount,'revenue_ratio':ratios[0],'market_cap_ratio':ratios[1],'backlog_ratio':ratios[2],'score':score}
def yahoo_chart(symbol,range_='3mo',interval='1d'):
 url=f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?range={range_}&interval={interval}'
 req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 OrangePiPaperAgent/3.1'});data=json.load(urllib.request.urlopen(req,timeout=20));r=data['chart']['result'][0];q=r['indicators']['quote'][0];return [(t,c,h,l) for t,c,h,l in zip(r.get('timestamp',[]),q.get('close',[]),q.get('high',[]),q.get('low',[])) if c is not None]
def priced_in(series,event_epoch):
 before=[(t,c) for t,c,_,_ in series if t<=event_epoch][-6:]
 if len(before)<2:return {'return_5d':None,'volatility':None,'penalty':0}
 returns=[before[i][1]/before[i-1][1]-1 for i in range(1,len(before))];move=before[-1][1]/before[0][1]-1;vol=statistics.pstdev(returns) if len(returns)>1 else 0;z=abs(move)/(vol*math.sqrt(len(returns)) or 1);penalty=5 if z>=2.5 else 3 if z>=1.5 else 0;return {'return_5d':move,'volatility':vol,'z':z,'penalty':penalty}
def regime(series):
 closes=[x[1] for x in series]
 if len(closes)<21:return 'UNKNOWN',None,None,None
 r20=closes[-1]/closes[-21]-1;r60=closes[-1]/closes[-min(61,len(closes))]-1;rets=[closes[i]/closes[i-1]-1 for i in range(max(1,len(closes)-20),len(closes))];vol=statistics.pstdev(rets)*math.sqrt(252);label='RISK_ON' if r20>0 and r60>0 else 'RISK_OFF' if r20<0 and r60<0 else 'MIXED';return label,r20,r60,vol
def observe(db,decision_id,ticker,entry_price,entry_epoch,benchmark='SPY'):
 stock=yahoo_chart(ticker);bench=yahoo_chart(benchmark);sm={dt.datetime.fromtimestamp(t,dt.timezone.utc).date():(c,h,l) for t,c,h,l in stock};bm={dt.datetime.fromtimestamp(t,dt.timezone.utc).date():c for t,c,_,_ in bench};dates=sorted(d for d in sm if d>=dt.datetime.fromtimestamp(entry_epoch,dt.timezone.utc).date() and d in bm)
 if not dates:return 0
 b0=bm[dates[0]];count=0
 for w in WINDOWS:
  if len(dates)<w:continue
  d=dates[w-1];price,_,_=sm[d];path=[sm[x] for x in dates[:w]];sr=price/entry_price-1;br=bm[d]/b0-1;mfe=max(x[1] for x in path)/entry_price-1;mae=min(x[2] for x in path)/entry_price-1;db.execute('INSERT OR REPLACE INTO outcome_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)',(decision_id,w,now(),price,benchmark,bm[d],sr,br,sr-br,mfe,mae));count+=1
 db.commit();return count
def report(db):
 rows=db.execute('SELECT window_days,stock_return,abnormal_return FROM outcome_observations WHERE abnormal_return IS NOT NULL').fetchall();out={}
 for w in WINDOWS:
  r=[x for x in rows if x[0]==w];a=[x[2] for x in r];s=[x[1] for x in r];n=len(a);out[str(w)]={'n':n,'mean_return':statistics.mean(s) if s else None,'mean_abnormal':statistics.mean(a) if a else None,'median_abnormal':statistics.median(a) if a else None,'win_rate':sum(x>0 for x in a)/n if n else None,'ci95':(1.96*statistics.stdev(a)/math.sqrt(n) if n>1 else None)}
 raw=json.dumps(out,sort_keys=True);db.execute('INSERT INTO research_reports VALUES(?,?,?)',(now(),VERSION,raw));db.commit();return out
