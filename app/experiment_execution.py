#!/usr/bin/env python3
import csv,datetime as dt,io,json,math,re,time,urllib.parse,urllib.request
UA='Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 OrangePiPaperAgent/2.2.1'
SCHEMA='CREATE TABLE IF NOT EXISTS quote_cache(ticker TEXT PRIMARY KEY,price REAL NOT NULL,source TEXT NOT NULL,quoted_at TEXT NOT NULL,updated_epoch REAL NOT NULL);'
def now():return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
def init(db):db.execute(SCHEMA);db.commit()
def get(url,timeout=12):
 req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'text/csv,text/html,*/*','Accept-Language':'en-US,en;q=0.8'})
 with urllib.request.urlopen(req,timeout=timeout) as r:return r.read(2000000),getattr(r,'status',200)
def yahoo_chart_quote(ticker):
 url=f'https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?range=5d&interval=1d';data,_=get(url);payload=json.loads(data);chart=payload.get('chart',{});error=chart.get('error');results=chart.get('result') or []
 if error or not results:raise ValueError('Yahoo chart missing result: '+str(error))
 result=results[0];meta=result.get('meta',{});price=meta.get('regularMarketPrice')
 if not isinstance(price,(int,float)) or not math.isfinite(price) or price<=0:
  closes=((result.get('indicators',{}).get('quote') or [{}])[0].get('close') or []);valid=[float(x) for x in closes if isinstance(x,(int,float)) and math.isfinite(x) and x>0]
  if not valid:raise ValueError('Yahoo chart contains no valid price')
  price=valid[-1]
 ts=meta.get('regularMarketTime');quoted=dt.datetime.fromtimestamp(ts,dt.timezone.utc).replace(microsecond=0).isoformat() if isinstance(ts,(int,float)) else now()
 return float(price),'YAHOO_CHART',url,quoted

def stooq_quote(ticker):
 url=f'https://stooq.com/q/?s={urllib.parse.quote(ticker.lower())}.us';data,_=get(url);text=data.decode('utf-8','replace')
 patterns=[r'Last[^0-9]{0,80}([0-9][0-9,]*(?:\.[0-9]+)?)',r'Apple Inc.*?([0-9][0-9,]*(?:\.[0-9]+)?)']
 for pattern in patterns:
  m=re.search(pattern,text,re.I|re.S)
  if m:
   price=float(m.group(1).replace(',',''))
   if math.isfinite(price) and price>0:return price,'STOOQ_PAGE',url,now()
 raise ValueError('Stooq quote-page price marker not found')

def google_exchange(x):
 x=(x or '').upper()
 if 'NASDAQ' in x:return 'NASDAQ'
 if 'AMERICAN' in x or x=='AMEX':return 'NYSEAMERICAN'
 if 'NYSE' in x:return 'NYSE'
 return 'NASDAQ'
def google_finance_quote(ticker,exchange):
 ex=google_exchange(exchange);url=f'https://www.google.com/finance/quote/{urllib.parse.quote(ticker)}:{ex}?hl=en';data,_=get(url);text=data.decode('utf-8','replace')
 for pattern in (r'data-last-price="([0-9][0-9,]*(?:\.[0-9]+)?)"',r'class="YMlKec fxKbKc">\$?([0-9][0-9,]*(?:\.[0-9]+)?)<'):
  m=re.search(pattern,text)
  if m:
   p=float(m.group(1).replace(',',''))
   if math.isfinite(p) and p>0:return p,'GOOGLE_FINANCE_PAGE',url
 raise ValueError('Google Finance price marker not found')
def cached_quote(db,ticker,max_age=86400):
 row=db.execute('select price,source,quoted_at,updated_epoch from quote_cache where ticker=?',(ticker,)).fetchone()
 if row and time.time()-float(row[3])<=max_age:return float(row[0]),'CACHE:'+row[1],row[2]
def resolve_quote(db,ticker,meta):
 init(db);errors=[]
 for fn in (lambda:yahoo_chart_quote(ticker),lambda:stooq_quote(ticker),lambda:google_finance_quote(ticker,meta.get('exchange',''))):
  try:
   result=fn(); price,source,detail=result[:3];ts=result[3] if len(result)>3 else now();db.execute('insert into quote_cache values(?,?,?,?,?) on conflict(ticker) do update set price=excluded.price,source=excluded.source,quoted_at=excluded.quoted_at,updated_epoch=excluded.updated_epoch',(ticker,price,source,ts,time.time()));db.commit();return {'price':price,'source':source,'quoted_at':ts,'detail':detail,'errors':errors}
  except Exception as e:errors.append(type(e).__name__+': '+str(e)[:120])
 q=cached_quote(db,ticker)
 if q:return {'price':q[0],'source':q[1],'quoted_at':q[2],'detail':'local cache','errors':errors}
 return {'price':None,'source':'NONE','quoted_at':None,'detail':None,'errors':errors}
def allocation_pct(score):
 return 0 if score<65 else .10 if score<80 else .20 if score<90 else .35 if score<95 else .50
def book_equity(store):return float(store.cash())+float(store.db.execute("select coalesce(sum(cost),0) from positions where status='OPEN'").fetchone()[0])
def exposures(store):
 rows=store.db.execute("select ticker,theme,cost from positions where status='OPEN'").fetchall();bt={};bh={}
 for r in rows:bt[r['ticker']]=bt.get(r['ticker'],0)+r['cost'];bh[r['theme']]=bh.get(r['theme'],0)+r['cost']
 return sum(r['cost'] for r in rows),bt,bh
def execute_virtual_buy(store,cfg,watch,ticker,score,decision_id):
 p=cfg['portfolio'];meta=watch[ticker];eq=book_equity(store);pct=allocation_pct(float(score));minimum=eq*.10;target=eq*pct
 if not pct:store.db.execute("update decisions set status='REJECTED',reason=reason||'; score below 65' where id=?",(decision_id,));store.db.commit();return
 q=resolve_quote(store.db,ticker,meta)
 if q['price'] is None:store.db.execute("update decisions set status='AWAITING_PRICE',amount=?,reason=reason||? where id=?",(target,'; awaiting price; '+', '.join(q['errors']),decision_id));store.db.commit();return
 total,bt,bh=exposures(store);cash=store.cash();theme=meta.get('theme','Unclassified');amount=min(target,eq*p['max_single_stock_pct']-bt.get(ticker,0),eq*p['max_theme_pct']-bh.get(theme,0),eq*p['max_deployed_pct']-total,cash)
 if amount+.01<minimum:store.db.execute("update decisions set status='CAPACITY_DEFERRED',amount=?,reason=reason||? where id=?",(target,f'; minimum={minimum:.2f}; capacity={amount:.2f}',decision_id));store.db.commit();return
 fill=q['price']*(1+p.get('slippage_bps',15)/10000);shares=math.floor(amount/fill)
 if shares<1:store.db.execute("update decisions set status='CAPACITY_DEFERRED' where id=?",(decision_id,));store.db.commit();return
 cost=round(shares*fill,2);ts=now()
 store.db.execute('begin immediate')
 try:
  store.db.execute("insert into ledger(created,kind,amount,ref,balance) values(?,?,?,?,?)",(ts,'VIRTUAL_BUY',-cost,f'{ticker}:{decision_id}',round(cash-cost,2)));store.db.execute("insert into positions(ticker,theme,opened,entry_price,shares,cost,decision_id,status) values(?,?,?,?,?,?,?,'OPEN')",(ticker,theme,ts,fill,shares,cost,decision_id));store.db.execute("update decisions set status='EXECUTED',entry_price=?,shares=?,amount=?,reason=reason||? where id=?",(fill,shares,cost,f'; quote={q["source"]}; quote_time={q["quoted_at"]}; allocation={pct:.0%}',decision_id));store.db.commit()
 except Exception:store.db.rollback();raise
def retry_pending(store,cfg,watch,limit=50):
 rows=store.db.execute("select id,ticker,score from decisions where action='BUY' and status in ('AWAITING_PRICE','CAPACITY_DEFERRED') order by id limit ?",(limit,)).fetchall();out={'retried':0,'executed':0,'awaiting_price':0,'capacity_deferred':0,'errors':0}
 for r in rows:
  out['retried']+=1
  try:
   store.db.execute("update decisions set status='PROPOSED' where id=?",(r['id'],));store.db.commit();execute_virtual_buy(store,cfg,watch,r['ticker'],r['score'],r['id']);st=store.db.execute('select status from decisions where id=?',(r['id'],)).fetchone()[0].lower();out[st]=out.get(st,0)+1
  except Exception as e:store.db.execute("update decisions set status='EXECUTION_ERROR',reason=reason||? where id=?",('; retry '+type(e).__name__,r['id']));store.db.commit();out['errors']+=1
 return out
