#!/usr/bin/env python3
import datetime as dt, hashlib, importlib.util, json, os, re, sqlite3, sys, time, urllib.error, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

UA='OrangePiPaperAgent/2.1.1 public-data-research local-admin'
SCHEMA='''
CREATE TABLE IF NOT EXISTS source_health(source_id TEXT PRIMARY KEY,name TEXT,last_attempt TEXT,last_success TEXT,last_status TEXT,failures INTEGER DEFAULT 0,backoff_until REAL DEFAULT 0,items_total INTEGER DEFAULT 0,last_error TEXT);
CREATE TABLE IF NOT EXISTS broad_state(source_id TEXT PRIMARY KEY,last_marker TEXT,updated TEXT);
'''
FORMS={'8-K','8-K/A','6-K','6-K/A','S-1','S-1/A','F-1','F-1/A','10-Q','10-K','4','SC 13D','SC 13G','424B2','424B3','424B4','424B5','NT 10-Q','NT 10-K','DEF 14A','SC TO-I','SC TO-T'}

def now(): return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
def req(url,data=None,headers=None,timeout=20):
 h={'User-Agent':UA,'Accept':'application/json,application/atom+xml,application/rss+xml,text/xml,*/*'}; h.update(headers or {})
 r=urllib.request.Request(url,data=data,headers=h,method='POST' if data is not None else 'GET')
 with urllib.request.urlopen(r,timeout=timeout) as x: return x.read(8_000_000),getattr(x,'status',200)
def strip_html(s): return re.sub(r'\s+',' ',re.sub(r'<[^>]+>',' ',s or '')).strip()
def local(tag): return tag.rsplit('}',1)[-1]
def xml_items(data):
 root=ET.fromstring(data); out=[]
 for el in root.iter():
  if local(el.tag) not in ('item','entry'): continue
  vals={}
  for c in list(el):
   k=local(c.tag); val=''.join(c.itertext()).strip()
   if k=='link' and not val: val=c.attrib.get('href','')
   vals.setdefault(k,val)
  title=vals.get('title',''); url=vals.get('link',''); summary=strip_html(vals.get('summary') or vals.get('description') or vals.get('content','')); pub=vals.get('updated') or vals.get('published') or vals.get('pubDate') or now()
  if title: out.append({'title':title,'summary':summary,'url':url,'published':pub})
 return out
def fetch_source(src):
 typ=src['type']
 if typ in ('rss','atom'):
  data,status=req(src['url']); items=xml_items(data)
  if src['id']=='sec_latest':
   for x in items:
    m=re.match(r'\s*(.+?)\s+-\s+(.+)',x['title']); x['form']=m.group(1).strip() if m else ''
   items=[x for x in items if x.get('form') in FORMS]
  return items,status
 if typ=='federal_register':
  data,status=req(src['url']); j=json.loads(data); rows=j.get('results',j.get('documents',[])); out=[]
  for x in rows:
   agency=', '.join(a.get('name','') for a in x.get('agencies',[]) if isinstance(a,dict))
   out.append({'title':x.get('title',''),'summary':' '.join(filter(None,[agency,x.get('abstract',''),x.get('document_type','')])), 'url':x.get('html_url') or x.get('pdf_url',''),'published':x.get('filed_at') or x.get('publication_date') or now()})
  return out,status
 if typ=='openfda':
  data,status=req(src['url']); j=json.loads(data); out=[]
  for x in j.get('results',[]):
   firm=x.get('recalling_firm',''); product=x.get('product_description',''); reason=x.get('reason_for_recall','')
   out.append({'title':f'{firm} recall: {product[:140]}','summary':' '.join(filter(None,[reason,x.get('classification',''),x.get('status','')])), 'url':'https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts/enforcement-reports','published':x.get('report_date') or now()})
  return out,status
 if typ=='usaspending':
  today=dt.datetime.now(dt.timezone.utc).date(); start=today-dt.timedelta(days=5)
  payload={'filters':{'time_period':[{'start_date':start.isoformat(),'end_date':today.isoformat()}],'award_type_codes':['A','B','C','D']},'fields':['Award ID','Recipient Name','Start Date','Award Amount','Awarding Agency','Description'],'page':1,'limit':100,'subawards':False}
  data,status=req(src['url'],json.dumps(payload).encode(),{'Content-Type':'application/json'}); j=json.loads(data); out=[]
  for x in j.get('results',[]):
   recipient=x.get('Recipient Name',''); amount=x.get('Award Amount'); desc=x.get('Description') or ''
   out.append({'title':f'{recipient} federal award {amount}','summary':' '.join(filter(None,[x.get('Awarding Agency',''),desc])), 'url':'https://www.usaspending.gov/award/'+str(x.get('Award ID','')),'published':x.get('Start Date') or now()})
  return out,status
 raise ValueError('unknown source type '+typ)
def load_core():
 p='/opt/paper-agent/app/agent.py'; spec=importlib.util.spec_from_file_location('pa_core',p); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
def init(db): db.executescript(SCHEMA); db.commit()
def health(db,src,status,error=None,count=0):
 t=now(); row=db.execute('select failures from source_health where source_id=?',(src['id'],)).fetchone(); failures=(row[0] if row else 0)
 if error: failures+=1; wait=min(21600,300*(2**min(failures,6))); back=time.time()+wait; success=None
 else: failures=0; back=0; success=t
 db.execute('''insert into source_health(source_id,name,last_attempt,last_success,last_status,failures,backoff_until,items_total,last_error) values(?,?,?,?,?,?,?,?,?) on conflict(source_id) do update set name=excluded.name,last_attempt=excluded.last_attempt,last_success=coalesce(excluded.last_success,source_health.last_success),last_status=excluded.last_status,failures=excluded.failures,backoff_until=excluded.backoff_until,items_total=source_health.items_total+excluded.items_total,last_error=excluded.last_error''',(src['id'],src['name'],t,success,str(status),failures,back,count,(str(error)[:300] if error else None))); db.commit()
def parse_iso_epoch(value):
 if not value: return 0.0
 try: return dt.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
 except (ValueError,TypeError): return 0.0
def source_due(db,src,clock=time.time):
 row=db.execute('select last_attempt,backoff_until from source_health where source_id=?',(src['id'],)).fetchone()
 if not row: return True,'never_run'
 current=clock()
 if float(row[1] or 0)>current: return False,'backoff'
 elapsed=current-parse_iso_epoch(row[0])
 return elapsed>=int(src.get('interval',600)),('interval_due' if elapsed>=int(src.get('interval',600)) else 'interval_wait')

def process(core,store,cfg,watch,src,item):
 title=item.get('title','').strip(); summary=item.get('summary','').strip(); url=item.get('url','').strip(); pub=item.get('published') or now()
 uid=hashlib.sha256((src['id']+'|'+url+'|'+title).encode()).hexdigest()
 try:
  store.db.execute('insert into news(uid,source,title,summary,url,published,ingested,trust) values(?,?,?,?,?,?,?,?)',(uid,src['name'],title,summary,url,pub,now(),src['trust'])); store.db.commit()
 except sqlite3.IntegrityError: return {'result':'duplicate'}
 blob=title+'. '+summary; tickers=core.map_tickers(blob,watch); score,reasons=core.score_event(blob,src['trust']); act=core.direction(blob)
 if not tickers:
  store.db.execute("insert into decisions(created,news_uid,ticker,action,score,reason,rules_version,status,theme) values(?,?,?,?,?,?,?,?,?)",(now(),uid,'','NO_TRADE',score,'No verified company mapped; '+'; '.join(reasons),store.rules_version(),'REJECTED','')); store.db.commit(); return {'result':'unmapped'}
 result={'result':'mapped','executed':0,'no_price':0,'risk_rejected':0,'execution_error':0}
 for ticker in tickers[:8]:
  action=act if score>=cfg['portfolio']['entry_score'] else 'NO_TRADE'; status='PROPOSED' if action=='BUY' else 'REJECTED'; meta=watch[ticker]
  cur=store.db.execute("insert into decisions(created,news_uid,ticker,action,score,reason,rules_version,status,theme) values(?,?,?,?,?,?,?,?,?)",(now(),uid,ticker,action,score,'; '.join(reasons),store.rules_version(),status,meta.get('theme','Unclassified'))); store.db.commit()
  if action!='BUY' or os.path.exists('/var/lib/paper-agent/PAUSE_TRADES'): continue
  try:
   core.execute_virtual_buy(store,cfg,watch,ticker,score,cur.lastrowid)
   final=store.db.execute('select status from decisions where id=?',(cur.lastrowid,)).fetchone()[0]
   if final=='EXECUTED': result['executed']+=1
   elif final=='NO_PRICE': result['no_price']+=1
   elif final=='RISK_REJECTED': result['risk_rejected']+=1
   elif final=='PROPOSED':
    store.db.execute("update decisions set status='EXECUTION_ERROR',reason=reason||'; executor returned without final status' where id=?",(cur.lastrowid,)); store.db.commit(); result['execution_error']+=1
  except Exception as e:
   status='NO_PRICE' if isinstance(e,(urllib.error.HTTPError,urllib.error.URLError,TimeoutError)) else 'EXECUTION_ERROR'
   store.db.execute("update decisions set status=?,reason=reason||? where id=?",(status,'; execution error: '+type(e).__name__+': '+str(e)[:180],cur.lastrowid)); store.db.commit()
   result['no_price' if status=='NO_PRICE' else 'execution_error']+=1
 return result

def cycle(config_path='/etc/paper-agent/broad-sources.json'):
 cfgsrc=json.load(open(config_path)); cfg=json.load(open('/etc/paper-agent/config.json')); watch=json.load(open('/etc/paper-agent/watchlist.json')); core=load_core(); store=core.Store('/var/lib/paper-agent/paper-agent.db',cfg['portfolio']['starting_cash']); init(store.db)
 totals={'sources_ok':0,'fetch_failures':0,'item_failures':0,'new':0,'mapped':0,'executed':0,'no_price':0,'risk_rejected':0,'execution_error':0,'duplicate':0,'unmapped':0,'sources_skipped':0}
 for src in cfgsrc['sources']:
  due,why=source_due(store.db,src)
  if not due: totals['sources_skipped']+=1; continue
  try:
   items,status=fetch_source(src); items=items[:cfgsrc.get('max_items_per_source',100)]
  except Exception as e:
   health(store.db,src,getattr(e,'code',type(e).__name__),e); totals['fetch_failures']+=1; continue
  health(store.db,src,status,count=len(items)); totals['sources_ok']+=1
  for item in items:
   try:
    r=process(core,store,cfg,watch,src,item); key=r.get('result','item_failures'); totals[key]=totals.get(key,0)+1
    if key!='duplicate': totals['new']+=1
    for k in ('executed','no_price','risk_rejected','execution_error'): totals[k]+=int(r.get(k,0))
   except Exception as e:
    totals['item_failures']+=1
    print(json.dumps({'item_error':{'source':src['id'],'type':type(e).__name__,'detail':str(e)[:180]}}),flush=True)
 
 try:
  from experiment_execution import retry_pending
  totals['pending_retry']=retry_pending(store,cfg,watch)
 except Exception as e: totals['pending_retry']={'error':type(e).__name__+': '+str(e)[:120]}
 print(json.dumps({'broad_cycle':totals,'time':now()}),flush=True); return totals

if __name__=='__main__': cycle(sys.argv[1] if len(sys.argv)>1 else '/etc/paper-agent/broad-sources.json')
