#!/usr/bin/env python3
import datetime as dt, hashlib, json, math, os, re, sqlite3, statistics, urllib.parse, urllib.request
MODEL_VERSION='3.0.0'
SCHEMA='''
CREATE TABLE IF NOT EXISTS model_versions(version TEXT PRIMARY KEY,created TEXT NOT NULL,parameters_json TEXT NOT NULL,parameters_hash TEXT NOT NULL,code_commit TEXT);
CREATE TABLE IF NOT EXISTS theses(decision_id INTEGER PRIMARY KEY,model_version TEXT NOT NULL,event_type TEXT NOT NULL,entity_certainty REAL NOT NULL,source_quality REAL NOT NULL,novelty REAL NOT NULL,materiality REAL NOT NULL,directional_clarity REAL NOT NULL,timing REAL NOT NULL,price_reaction REAL NOT NULL,confirmation REAL NOT NULL,composite_score REAL NOT NULL,event_key TEXT NOT NULL,source_published TEXT,decision_time TEXT NOT NULL,stale INTEGER NOT NULL DEFAULT 0,duplicate_event INTEGER NOT NULL DEFAULT 0,thesis_json TEXT NOT NULL,immutable_hash TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_thesis_event_ticker ON theses(event_key,decision_id);
CREATE TABLE IF NOT EXISTS price_observations(ticker TEXT NOT NULL,observed_at TEXT NOT NULL,price REAL NOT NULL,source TEXT NOT NULL,PRIMARY KEY(ticker,observed_at,source));
CREATE TABLE IF NOT EXISTS outcome_windows(decision_id INTEGER NOT NULL,window_days INTEGER NOT NULL,observed_at TEXT NOT NULL,stock_return REAL,market_return REAL,abnormal_return REAL,mfe REAL,mae REAL,PRIMARY KEY(decision_id,window_days));
CREATE TABLE IF NOT EXISTS strategy_metrics(calculated_at TEXT NOT NULL,model_version TEXT NOT NULL,group_type TEXT NOT NULL,group_value TEXT NOT NULL,trades INTEGER NOT NULL,win_rate REAL,mean_return REAL,median_return REAL,mean_abnormal_return REAL,profit_factor REAL,max_drawdown REAL);
CREATE TABLE IF NOT EXISTS invariant_events(created TEXT NOT NULL,code TEXT NOT NULL,detail TEXT NOT NULL,severity TEXT NOT NULL);
'''
PARAMETERS={'entry_score':65,'allocations':{'65-79':.10,'80-89':.20,'90-94':.35,'95-100':.50},'single_stock_max':.50,'theme_max':.80,'deployment_max':.90,'holding_days':15,'hard_stop':.12,'stale_hours':72,'entity_min':16}
PRIMARY={'SEC EDGAR Latest Filings':15,'Federal Register Public Inspection':14,'USAspending New Awards':14,'FTC Press Releases':14,'openFDA Drug Enforcement':13,'openFDA Device Enforcement':13,'NVIDIA Newsroom':13}
MEDIA={'TechCrunch':9,'Ars Technica':9,'The Verge':8,'Techmeme':7,'Hacker News':5,'GeekWire':8}
EVENTS=[('government_contract',r'\b(award|contract|task order|obligation|procurement)\b'),('fda_regulatory',r'\b(fda|approval|clearance|clinical trial|pdufa|recall)\b'),('earnings_guidance',r'\b(earnings|revenue|guidance|forecast|quarter)\b'),('insider_ownership',r'\b(form 4|insider|13d|13g|beneficial owner)\b'),('merger_transaction',r'\b(acquisition|acquire|merger|tender offer|buyout)\b'),('capital_dilution',r'\b(offering|prospectus|dilution|convertible|atm program)\b'),('product_customer',r'\b(launch|customer|partnership|deployment|order)\b'),('cyber_incident',r'\b(cyber|breach|ransomware|vulnerability|security incident)\b'),('regulation_policy',r'\b(rule|tariff|export control|sanction|regulation)\b')]
POSITIVE=re.compile(r'\b(award|wins?|approval|approved|launch|raises guidance|partnership|contract|order|clearance|acquisition interest)\b',re.I)
NEGATIVE=re.compile(r'\b(recall|breach|investigation|cuts guidance|bankrupt|default|dilution|offering|suspension|fraud)\b',re.I)
FORWARD=re.compile(r'\b(will|expects?|scheduled|effective|due|launch|decision|hearing|trial readout)\b',re.I)

def now():return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
def init(db,commit='unknown'):
 db.executescript(SCHEMA);raw=json.dumps(PARAMETERS,sort_keys=True,separators=(',',':'));h=hashlib.sha256(raw.encode()).hexdigest();db.execute('insert or ignore into model_versions values(?,?,?,?,?)',(MODEL_VERSION,now(),raw,h,commit));db.commit()
def parse_time(value):
 if not value:return None
 try:
  x=dt.datetime.fromisoformat(str(value).replace('Z','+00:00'));return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)
 except ValueError:
  try:return dt.datetime.strptime(value,'%a, %d %b %Y %H:%M:%S %Z').replace(tzinfo=dt.timezone.utc)
  except ValueError:return None
def event_type(text):
 for name,pattern in EVENTS:
  if re.search(pattern,text,re.I):return name
 return 'general_news'
def source_points(name,trust):return min(15,max(PRIMARY.get(name,MEDIA.get(name,round(float(trust)*10))),0))
def entity_points(ticker,meta,text):
 if not ticker:return 0
 name=(meta.get('name') or '').lower();aliases=[str(x).lower() for x in meta.get('aliases',[])];low=text.lower()
 if name and len(name)>=5 and name in low:return 20
 if any(len(a)>=6 and a in low for a in aliases):return 17
 return 16 if len(ticker)>=2 and re.search(r'(?<![A-Z])'+re.escape(ticker)+r'(?![A-Z])',text) else 10
def event_key(ticker,etype,title,summary):
 normalized=re.sub(r'\b\d+(?:\.\d+)?\b','#',re.sub(r'[^a-z0-9 ]',' ',(title+' '+summary).lower()));tokens=sorted(set(x for x in normalized.split() if len(x)>3))[:30];return hashlib.sha256((ticker+'|'+etype+'|'+' '.join(tokens)).encode()).hexdigest()
def materiality_points(text,etype):
 amounts=[]
 for raw,unit in re.findall(r'\$?([0-9]+(?:\.[0-9]+)?)\s*(billion|million|bn|m)\b',text,re.I):
  x=float(raw)*(1e9 if unit.lower() in ('billion','bn') else 1e6);amounts.append(x)
 if amounts:
  x=max(amounts)
  return 20 if x>=1e9 else 17 if x>=250e6 else 14 if x>=50e6 else 10
 return 12 if etype in ('fda_regulatory','merger_transaction') else 6
def directional_points(text):
 p=len(POSITIVE.findall(text));n=len(NEGATIVE.findall(text))
 if p and not n:return 10
 if n and not p:return 9
 if p or n:return 5
 return 2
def evaluate(db,ticker,meta,source,trust,title,summary,published,decision_time=None):
 init(db);decision_time=decision_time or now();text=title+' '+summary;etype=event_type(text);entity=entity_points(ticker,meta,text);sp=source_points(source,trust);mat=materiality_points(text,etype);direction=directional_points(text);timing=10 if FORWARD.search(text) else 6;published_dt=parse_time(published);decision_dt=parse_time(decision_time);age_h=(decision_dt-published_dt).total_seconds()/3600 if published_dt and decision_dt else 0;stale=int(age_h>PARAMETERS['stale_hours']);novelty=0 if stale else 15 if age_h<=6 else 12 if age_h<=24 else 8;key=event_key(ticker,etype,title,summary);duplicate=int(db.execute('select count(*) from theses where event_key=?',(key,)).fetchone()[0]>0);confirmation=5 if source in PRIMARY else 2;price_reaction=3;score=entity+sp+novelty+mat+direction+timing+price_reaction+confirmation
 if entity<PARAMETERS['entity_min'] or stale or duplicate:score=min(score,64)
 thesis={'event_type':etype,'entity_evidence':entity,'source_quality':sp,'novelty':novelty,'materiality':mat,'directional_clarity':direction,'timing':timing,'price_reaction_placeholder':price_reaction,'confirmation':confirmation,'age_hours':round(age_h,2),'stale':bool(stale),'duplicate_event':bool(duplicate),'rationale':'Structured deterministic thesis; materiality uses disclosed amounts when present.'};raw=json.dumps(thesis,sort_keys=True,separators=(',',':'));return {'score':float(min(100,score)),'event_type':etype,'event_key':key,'stale':stale,'duplicate':duplicate,'thesis':thesis,'hash':hashlib.sha256(raw.encode()).hexdigest(),'components':(entity,sp,novelty,mat,direction,timing,price_reaction,confirmation),'json':raw,'published':published,'decision_time':decision_time}
def record(db,decision_id,result):
 e,s,n,m,d,t,p,c=result['components'];db.execute('insert or replace into theses values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(decision_id,MODEL_VERSION,result['event_type'],e,s,n,m,d,t,p,c,result['score'],result['event_key'],result['published'],result['decision_time'],result['stale'],result['duplicate'],result['json'],result['hash']));db.commit()
def check_invariants(db,cfg):
 events=[];cash=db.execute('select balance from ledger order by id desc limit 1').fetchone();cash=float(cash[0]) if cash else 0;rows=db.execute("select ticker,theme,cost from positions where status='OPEN'").fetchall();deployed=sum(float(r['cost']) for r in rows);equity=cash+deployed
 if cash<-0.01:events.append(('NEGATIVE_CASH',f'cash={cash}','CRITICAL'))
 if equity>0 and deployed/equity>cfg['portfolio']['max_deployed_pct']+.0001:events.append(('DEPLOYMENT_LIMIT',f'{deployed/equity:.4f}','CRITICAL'))
 stranded=db.execute("select count(*) from decisions where action='BUY' and status='PROPOSED'").fetchone()[0]
 if stranded:events.append(('STRANDED_PROPOSALS',str(stranded),'HIGH'))
 for code,detail,severity in events:db.execute('insert into invariant_events values(?,?,?,?)',(now(),code,detail,severity))
 db.commit();return events
def performance_report(db):
 rows=db.execute("select d.id,d.score,d.status,p.pnl,p.cost,n.source,t.event_type,t.model_version from decisions d left join positions p on p.decision_id=d.id left join news n on n.uid=d.news_uid left join theses t on t.decision_id=d.id where d.action='BUY'").fetchall();groups={}
 for r in rows:
  key=(r['source'] or 'Unknown',r['event_type'] or 'legacy');groups.setdefault(key,[]).append(r)
 out=[]
 for (source,event),g in groups.items():
  returns=[float(x['pnl'])/float(x['cost']) for x in g if x['pnl'] is not None and x['cost']]
  out.append({'source':source,'event_type':event,'signals':len(g),'closed':len(returns),'win_rate':(sum(x>0 for x in returns)/len(returns) if returns else None),'mean_return':(statistics.mean(returns) if returns else None),'median_return':(statistics.median(returns) if returns else None)})
 return sorted(out,key=lambda x:(-x['closed'],-x['signals']))
