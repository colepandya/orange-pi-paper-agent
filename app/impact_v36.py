#!/usr/bin/env python3
"""Paper Agent V3.6 event-impact forecasting and learning engine. Virtual only."""
import datetime as dt,hashlib,json,math,os,re,sqlite3,statistics
VERSION='3.6.0';DB='/var/lib/paper-agent/paper-agent.db';WINDOWS=(1,3,5,10,15)
SCHEMA='''
CREATE TABLE IF NOT EXISTS v36_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_impact_forecasts(
 decision_id INTEGER PRIMARY KEY,created TEXT NOT NULL,forecast_version TEXT NOT NULL,event_type TEXT NOT NULL,
 eligibility REAL NOT NULL,impact_score REAL NOT NULL,confidence REAL NOT NULL,data_completeness REAL NOT NULL,
 economic_impact REAL NOT NULL,priced_in REAL NOT NULL,residual_return REAL NOT NULL,bear_return REAL NOT NULL,
 base_return REAL NOT NULL,bull_return REAL NOT NULL,expected_return REAL NOT NULL,target_return REAL NOT NULL,
 stop_return REAL NOT NULL,max_hold_days INTEGER NOT NULL,target_probability REAL NOT NULL,allocation_pct REAL NOT NULL,
 partial_exit_pct REAL NOT NULL,runner_target REAL NOT NULL,runner_stop REAL NOT NULL,decision TEXT NOT NULL,
 invalidation_json TEXT NOT NULL,inputs_json TEXT NOT NULL,derivation_json TEXT NOT NULL,forecast_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_plans(
 decision_id INTEGER NOT NULL,policy TEXT NOT NULL,created TEXT NOT NULL,target_return REAL NOT NULL,stop_return REAL NOT NULL,
 max_hold_days INTEGER NOT NULL,partial_exit_pct REAL NOT NULL,runner_target REAL,runner_stop REAL,status TEXT NOT NULL DEFAULT 'OPEN',
 outcome_return REAL,capital_days REAL,trigger TEXT,closed_at TEXT,PRIMARY KEY(decision_id,policy));
CREATE TABLE IF NOT EXISTS forecast_observations(
 decision_id INTEGER NOT NULL,window_days INTEGER NOT NULL,observed_at TEXT NOT NULL,stock_return REAL,spy_return REAL,
 sector_return REAL,spy_abnormal REAL,sector_abnormal REAL,mfe REAL,mae REAL,forecast_error REAL,range_covered INTEGER,
 PRIMARY KEY(decision_id,window_days));
CREATE TABLE IF NOT EXISTS forecast_calibration(
 calculated_at TEXT NOT NULL,forecast_version TEXT NOT NULL,group_type TEXT NOT NULL,group_value TEXT NOT NULL,sample_size INTEGER NOT NULL,
 mean_expected REAL,mean_actual REAL,mae_forecast REAL,range_coverage REAL,target_calibration REAL,detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS issuer_exposure_snapshots(
 observed_at TEXT NOT NULL,ticker TEXT NOT NULL,open_lots INTEGER NOT NULL,cost REAL NOT NULL,equity_pct REAL NOT NULL,
 theme TEXT,theme_cost REAL,PRIMARY KEY(observed_at,ticker));
CREATE INDEX IF NOT EXISTS idx_forecast_event ON event_impact_forecasts(event_type,created);
CREATE INDEX IF NOT EXISTS idx_forecast_decision ON event_impact_forecasts(decision,target_probability);
CREATE INDEX IF NOT EXISTS idx_shadow_policy ON shadow_plans(policy,status);
'''
EVENT_PRIORS={
 'government_contract':(.045,.035,4),'product_customer':(.035,.030,3),'earnings_guidance':(.050,.040,5),
 'fda_regulatory':(.075,.055,5),'merger_transaction':(.025,.060,15),'capital_dilution':(-.045,.050,3),
 'cyber_incident':(-.055,.055,3),'regulation_policy':(.025,.040,5),'insider_ownership':(.025,.030,5),'general_news':(.015,.030,2)}
AMOUNT=re.compile(r'\$?([0-9]+(?:\.[0-9]+)?)\s*(billion|million|bn|m)\b',re.I)
def now():return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),default=str)
def sha(x):return hashlib.sha256((x if isinstance(x,bytes) else str(x).encode())).hexdigest()
def connect(path=DB):
 d=sqlite3.connect(path,timeout=60);d.row_factory=sqlite3.Row;d.execute('PRAGMA busy_timeout=60000');d.execute('PRAGMA foreign_keys=ON');return d
def init(d):d.executescript(SCHEMA);d.execute('INSERT OR REPLACE INTO v36_meta VALUES(?,?)',('version',VERSION));d.commit()
def clamp(v,lo,hi):return min(hi,max(lo,float(v)))
def disclosed_amount(text):
 xs=[]
 for v,u in AMOUNT.findall(text or ''):xs.append(float(v)*(1e9 if u.lower() in ('billion','bn') else 1e6))
 return max(xs) if xs else None
def fundamentals(d,ticker):
 if not d.execute("SELECT 1 FROM sqlite_master WHERE name='company_fundamentals'").fetchone():return {}
 r=d.execute('SELECT * FROM company_fundamentals WHERE ticker=?',(ticker,)).fetchone();return dict(r) if r else {}
def analogue_stats(d,event_type):
 rows=d.execute('''SELECT o.sector_abnormal FROM forecast_observations o JOIN event_impact_forecasts f ON f.decision_id=o.decision_id WHERE f.event_type=? AND o.window_days=5 AND o.sector_abnormal IS NOT NULL''',(event_type,)).fetchall()
 vals=[float(x[0]) for x in rows]
 if len(vals)<5:return {'n':len(vals),'median':None,'q25':None,'q75':None,'dispersion':None}
 vals.sort();q=lambda p:vals[min(len(vals)-1,max(0,round((len(vals)-1)*p)))]
 return {'n':len(vals),'median':statistics.median(vals),'q25':q(.25),'q75':q(.75),'dispersion':statistics.pstdev(vals)}
def materiality(event,text,f):
 amount=disclosed_amount(text);ratios={}
 if amount:
  for k in ('revenue','market_cap','backlog'):
   if f.get(k):ratios[k]=amount/float(f[k])
 ratio=max(ratios.values()) if ratios else None
 if event=='merger_transaction':return amount,ratio,.08 if amount else .03
 if event in ('capital_dilution','cyber_incident'):return amount,ratio,-clamp((ratio or .03)*.8,.02,.15)
 if ratio is not None:return amount,ratio,clamp(.012+ratio*.35,.012,.18)
 return amount,ratio,EVENT_PRIORS.get(event,EVENT_PRIORS['general_news'])[0]
def forecast(d,row):
 z=dict(row);event=z.get('event_type') or 'general_news';text=' '.join(str(z.get(k) or '') for k in ('title','summary','reason','thesis_json'));f=fundamentals(d,z.get('ticker'));amount,ratio,economic=materiality(event,text,f);prior,prior_risk,prior_days=EVENT_PRIORS.get(event,EVENT_PRIORS['general_news']);analogue=analogue_stats(d,event)
 thesis={}
 try:thesis=json.loads(z.get('thesis_json') or '{}')
 except Exception:pass
 eligibility=clamp(z.get('score') or 0,0,100);source_quality=clamp(thesis.get('source_quality',8)/15,0,1);entity=clamp(thesis.get('entity_evidence',16)/20,0,1);novelty=clamp(thesis.get('novelty',8)/15,0,1);completeness=sum(x is not None for x in (f.get('revenue'),f.get('market_cap'),f.get('shares'),amount,analogue.get('median')))/5
 confidence=100*clamp(.20+.22*source_quality+.18*entity+.15*novelty+.15*completeness+.10*min(1,analogue['n']/30),.15,.92)
 historical=analogue['median'] if analogue['median'] is not None else prior;five_day_move=0
 try:five_day_move=float(thesis.get('priced_in_return') or 0)
 except Exception:pass
 reaction_penalty=clamp(max(0,five_day_move),0,.20);expected_total=.55*economic+.45*historical;priced_in=reaction_penalty;residual=expected_total-priced_in;uncertainty=analogue['dispersion'] if analogue['dispersion'] is not None else prior_risk
 bear=residual-1.25*uncertainty;base=residual;bull=residual+1.50*uncertainty;expected=.20*bear+.55*base+.25*bull;cost=.003;uncertainty_discount=(1-confidence/100)*abs(uncertainty)*.6;target=max(0,base-uncertainty_discount-cost);stop=clamp(max(.015,abs(bear)*.55),.015,.08);target=clamp(target,.012,.15) if residual>0 else 0
 target_prob=clamp(.50+(expected/(abs(expected)+stop+.001))*.28+(confidence-50)/250,.05,.92);days=max(1,min(15,round(prior_days*(1.15-confidence/200))))
 decision='BUY' if eligibility>=65 and residual>=.012 and target_prob>=.52 and confidence>=45 else 'WATCH' if eligibility>=65 and residual>0 else 'REJECT_ALREADY_PRICED' if priced_in>=expected_total and expected_total>0 else 'REJECT'
 edge=max(0,expected-cost);allocation=clamp(edge*2.2*target_prob*(confidence/100)/(stop+.02),.03,.20) if decision=='BUY' else 0
 partial=.70 if decision=='BUY' and target>=.025 else 1.0;runner=target*1.75 if partial<1 else target;runner_stop=.005 if partial<1 else -stop
 invalid=['event facts contradicted or corrected','entity mapping invalidated','fresh quote unavailable','sector-adjusted reaction contradicts thesis','new dilution or adverse filing']
 inputs={'ticker':z.get('ticker'),'event_type':event,'amount':amount,'materiality_ratio':ratio,'fundamentals':f,'analogue':analogue,'source_quality':source_quality,'entity_certainty':entity,'novelty':novelty,'priced_in_input':five_day_move,'estimated_cost':cost}
 deriv={'economic_impact':economic,'historical_impact':historical,'expected_total_impact':expected_total,'priced_in':priced_in,'residual_return':residual,'uncertainty':uncertainty,'confidence':confidence,'target_formula':'base residual - uncertainty discount - costs','allocation_formula':'edge x probability x confidence / downside; capped at 20%'}
 payload=(row['id'],now(),VERSION,event,eligibility,100*clamp(abs(economic)/.15,0,1),confidence,100*completeness,economic,priced_in,residual,bear,base,bull,expected,target,-stop,days,target_prob,allocation,partial,runner,runner_stop,decision,canonical(invalid),canonical(inputs),canonical(deriv))
 return payload+(sha(canonical(payload)),)
def generate_missing(d,limit=1000):
 init(d);rows=d.execute('''SELECT x.*,n.source,n.title,n.summary,n.url,n.published,t.event_type,t.thesis_json FROM decisions x LEFT JOIN news n ON n.uid=x.news_uid LEFT JOIN theses t ON t.decision_id=x.id LEFT JOIN event_impact_forecasts f ON f.decision_id=x.id WHERE f.decision_id IS NULL AND x.ticker<>'' ORDER BY x.id LIMIT ?''',(limit,)).fetchall();n=0
 for row in rows:
  p=forecast(d,row);d.execute('INSERT INTO event_impact_forecasts VALUES('+','.join('?'*len(p))+')',p);f=d.execute('SELECT * FROM event_impact_forecasts WHERE decision_id=?',(row['id'],)).fetchone();plans=[('champion_fixed',.15,.10,10,1,None,None),('fast_fixed',.05,.03,3,1,None,None),('dynamic_v36',f['target_return'],abs(f['stop_return']),f['max_hold_days'],f['partial_exit_pct'],f['runner_target'],f['runner_stop'])]
  for policy,target,stop,days,partial,runner,rstop in plans:d.execute('INSERT OR IGNORE INTO shadow_plans(decision_id,policy,created,target_return,stop_return,max_hold_days,partial_exit_pct,runner_target,runner_stop) VALUES(?,?,?,?,?,?,?,?,?)',(row['id'],policy,now(),target,stop,days,partial,runner,rstop))
  n+=1
 d.commit();return n
def issuer_exposure(d):
 init(d);cash=d.execute('SELECT balance FROM ledger ORDER BY id DESC LIMIT 1').fetchone();cash=float(cash[0]) if cash else 0;rows=d.execute("SELECT ticker,theme,COUNT(*) lots,SUM(cost) cost FROM positions WHERE status='OPEN' GROUP BY ticker,theme").fetchall();total=sum(float(x['cost']) for x in rows);equity=cash+total;stamp=now()
 themes={}
 for x in rows:themes[x['theme']]=themes.get(x['theme'],0)+float(x['cost'])
 for x in rows:d.execute('INSERT INTO issuer_exposure_snapshots VALUES(?,?,?,?,?,?,?)',(stamp,x['ticker'],x['lots'],x['cost'],float(x['cost'])/equity if equity else 0,x['theme'],themes[x['theme']]))
 d.commit();return [dict(x) for x in rows]
def calibration(d):
 rows=d.execute('''SELECT f.event_type,f.expected_return,f.target_probability,o.sector_abnormal FROM event_impact_forecasts f JOIN forecast_observations o ON o.decision_id=f.decision_id AND o.window_days=5 WHERE o.sector_abnormal IS NOT NULL''').fetchall();groups={}
 for r in rows:groups.setdefault(r['event_type'],[]).append(r)
 out=[]
 for event,g in groups.items():
  actual=[float(x['sector_abnormal']) for x in g];expected=[float(x['expected_return']) for x in g];target=[float(x['target_probability']) for x in g];detail={'mean_target_probability':statistics.mean(target),'actual_positive_rate':sum(x>0 for x in actual)/len(actual)};row=(now(),VERSION,'event_type',event,len(g),statistics.mean(expected),statistics.mean(actual),statistics.mean(abs(a-e) for a,e in zip(actual,expected)),None,abs(detail['mean_target_probability']-detail['actual_positive_rate']),canonical(detail));d.execute('INSERT INTO forecast_calibration VALUES(?,?,?,?,?,?,?,?,?,?,?)',row);out.append(dict(event=event,n=len(g),**detail))
 d.commit();return out
