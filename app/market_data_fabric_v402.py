#!/usr/bin/env python3
"""Documented multi-provider quote fabric. Virtual-only and fail-closed."""
import concurrent.futures,datetime as dt,json,math,os,sqlite3,time,urllib.parse,urllib.request
try:
 from .market_clock_v401 import state
except ImportError: from market_clock_v401 import state
DB=os.getenv('PAPER_AGENT_DB','/var/lib/paper-agent/paper-agent.db');MAX_DEV=.005

def get_json(url,headers=None,timeout=12):
 req=urllib.request.Request(url,headers={'User-Agent':'PaperAgent/4.0.2 virtual-research',**(headers or {})})
 with urllib.request.urlopen(req,timeout=timeout) as r:return json.load(r),getattr(r,'status',200)
def iso_epoch(v):
 if v is None:return None
 if isinstance(v,(int,float)):return dt.datetime.fromtimestamp(v/1000 if v>1e12 else v,dt.timezone.utc).isoformat()
 return str(v).replace('Z','+00:00')
def obs(provider,ticker,price=None,bid=None,ask=None,stamp=None,returned=None,source=None,error=None,http=None):
 return {'provider':provider,'ticker':ticker,'returned_ticker':returned or ticker,'price':float(price) if price not in (None,'') else None,'bid':float(bid) if bid not in (None,'') else None,'ask':float(ask) if ask not in (None,'') else None,'provider_timestamp':iso_epoch(stamp),'received_timestamp':dt.datetime.now(dt.timezone.utc).isoformat(),'source':source or provider,'http_status':http,'error':error}
def twelve(t):
 k=os.getenv('TWELVE_DATA_API_KEY');
 if not k:return obs('TWELVE_DATA',t,error='DISABLED_NO_KEY')
 j,h=get_json('https://api.twelvedata.com/quote?symbol='+urllib.parse.quote(t)+'&apikey='+urllib.parse.quote(k));return obs('TWELVE_DATA',t,j.get('close'),j.get('bid'),j.get('ask'),j.get('timestamp'),j.get('symbol'),http=h)
def finnhub(t):
 k=os.getenv('FINNHUB_API_KEY');
 if not k:return obs('FINNHUB',t,error='DISABLED_NO_KEY')
 j,h=get_json('https://finnhub.io/api/v1/quote?symbol='+urllib.parse.quote(t)+'&token='+urllib.parse.quote(k));return obs('FINNHUB',t,j.get('c'),stamp=j.get('t'),http=h)
def alpaca(t):
 k=os.getenv('ALPACA_API_KEY');s=os.getenv('ALPACA_API_SECRET');
 if not k or not s:return obs('ALPACA',t,error='DISABLED_NO_KEY')
 j,h=get_json('https://data.alpaca.markets/v2/stocks/'+urllib.parse.quote(t)+'/snapshot',{'APCA-API-KEY-ID':k,'APCA-API-SECRET-KEY':s});q=j.get('latestQuote') or {};tr=j.get('latestTrade') or {};return obs('ALPACA',t,tr.get('p'),q.get('bp'),q.get('ap'),tr.get('t'),http=h)
def massive(t):
 k=os.getenv('MASSIVE_API_KEY');
 if not k:return obs('MASSIVE',t,error='DISABLED_NO_KEY')
 j,h=get_json('https://api.massive.com/v2/last/trade/'+urllib.parse.quote(t)+'?apiKey='+urllib.parse.quote(k));r=j.get('results') or {};return obs('MASSIVE',t,r.get('p'),stamp=r.get('t'),http=h)
def alpha(t):
 k=os.getenv('ALPHA_VANTAGE_API_KEY');
 if not k:return obs('ALPHA_VANTAGE',t,error='DISABLED_NO_KEY')
 j,h=get_json('https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol='+urllib.parse.quote(t)+'&apikey='+urllib.parse.quote(k));r=j.get('Global Quote') or {};return obs('ALPHA_VANTAGE',t,r.get('05. price'),stamp=None,http=h)
def tiingo(t):
 k=os.getenv('TIINGO_API_KEY');
 if not k:return obs('TIINGO',t,error='DISABLED_NO_KEY')
 j,h=get_json('https://api.tiingo.com/iex/'+urllib.parse.quote(t),{'Authorization':'Token '+k});r=j[0] if isinstance(j,list) and j else {};return obs('TIINGO',t,r.get('last') or r.get('tngoLast'),r.get('bidPrice'),r.get('askPrice'),r.get('timestamp'),r.get('ticker'),http=h)
def eodhd(t):
 k=os.getenv('EODHD_API_KEY');
 if not k:return obs('EODHD',t,error='DISABLED_NO_KEY')
 j,h=get_json('https://eodhd.com/api/real-time/'+urllib.parse.quote(t)+'.US?api_token='+urllib.parse.quote(k)+'&fmt=json');return obs('EODHD',t,j.get('close'),stamp=j.get('timestamp'),returned=j.get('code','').split('.')[0] or t,http=h)
PROVIDERS=[twelve,finnhub,alpaca,massive,alpha,tiingo,eodhd]
def validate(x,now=None):
 now=now or dt.datetime.now(dt.timezone.utc);p=x.get('price');x['identity_verified']=x.get('returned_ticker')==x['ticker']
 try:ts=dt.datetime.fromisoformat(x['provider_timestamp']) if x.get('provider_timestamp') else None
 except ValueError:ts=None
 age=(now-ts).total_seconds() if ts else None;x['age_seconds']=age
 valid=isinstance(p,(int,float)) and math.isfinite(p) and p>0;spread=not(x.get('bid') and x.get('ask')) or x['bid']<=x['ask'] and (x['ask']-x['bid'])/p<=.02
 x['accepted']=bool(not x.get('error') and x['identity_verified'] and valid and spread and (age is None or -300<=age<=900));x['reject_reason']=x.get('error') or (None if x['accepted'] else 'VALIDATION_FAILED');return x
def consensus(ticker,now=None):
 with concurrent.futures.ThreadPoolExecutor(max_workers=len(PROVIDERS)) as ex:
  fut=[ex.submit(p,ticker) for p in PROVIDERS];raw=[]
  for f in fut:
   try:raw.append(validate(f.result(),now))
   except Exception as e:raw.append(validate(obs('UNKNOWN',ticker,error=type(e).__name__),now))
 good=[x for x in raw if x['accepted']];prices=sorted(x['price'] for x in good);median=prices[len(prices)//2] if prices else None
 agreed=[x for x in good if median and abs(x['price']/median-1)<=MAX_DEV];dev=max((abs(x['price']/median-1) for x in agreed),default=None)
 status='PASS' if len(agreed)>=2 else ('VALUATION_ONLY' if len(agreed)==1 else 'FAILED');clk=state(now)
 return {'ticker':ticker,'session':clk['session'],'status':status,'consensus_price':median if agreed else None,'providers_used':[x['provider'] for x in agreed],'provider_count':len(agreed),'max_deviation':dev,'execution_eligible':int(status=='PASS' and clk['execution']),'observations':raw}
def init(d):
 d.executescript('''CREATE TABLE IF NOT EXISTS quote_consensus(id INTEGER PRIMARY KEY,ticker TEXT,created TEXT,session TEXT,status TEXT,consensus_price REAL,provider_count INTEGER,max_deviation REAL,execution_eligible INTEGER,providers_json TEXT,observations_json TEXT);CREATE INDEX IF NOT EXISTS ix_quote_consensus ON quote_consensus(ticker,id DESC);CREATE TABLE IF NOT EXISTS provider_health(provider TEXT PRIMARY KEY,requests INTEGER DEFAULT 0,successes INTEGER DEFAULT 0,failures INTEGER DEFAULT 0,auth_failures INTEGER DEFAULT 0,rate_limits INTEGER DEFAULT 0,last_success TEXT,last_error TEXT,state TEXT NOT NULL DEFAULT 'HEALTHY');CREATE TABLE IF NOT EXISTS quote_readiness(id INTEGER PRIMARY KEY,created TEXT,open_positions INTEGER,covered INTEGER,execution_eligible INTEGER,status TEXT,detail_json TEXT);''');d.commit()
def store(d,c):
 now=dt.datetime.now(dt.timezone.utc).isoformat();d.execute('INSERT INTO quote_consensus(ticker,created,session,status,consensus_price,provider_count,max_deviation,execution_eligible,providers_json,observations_json) VALUES(?,?,?,?,?,?,?,?,?,?)',(c['ticker'],now,c['session'],c['status'],c['consensus_price'],c['provider_count'],c['max_deviation'],c['execution_eligible'],json.dumps(c['providers_used']),json.dumps(c['observations'],sort_keys=True)))
 for x in c['observations']:
  p=x['provider'];d.execute('INSERT OR IGNORE INTO provider_health(provider) VALUES(?)',(p,));d.execute('UPDATE provider_health SET requests=requests+1,successes=successes+?,failures=failures+?,auth_failures=auth_failures+?,rate_limits=rate_limits+?,last_success=CASE WHEN ? THEN ? ELSE last_success END,last_error=?,state=? WHERE provider=?',(int(x['accepted']),int(not x['accepted']),int(x.get('http_status') in (401,403)),int(x.get('http_status')==429),int(x['accepted']),now,x.get('reject_reason'),('AUTH_FAILED' if x.get('http_status') in (401,403) else 'RATE_LIMITED' if x.get('http_status')==429 else 'HEALTHY' if x['accepted'] else 'DEGRADED'),p))
 if c['consensus_price']:
  d.execute('INSERT INTO quote_cache(ticker,price,source,quoted_at,updated_epoch) VALUES(?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET price=excluded.price,source=excluded.source,quoted_at=excluded.quoted_at,updated_epoch=excluded.updated_epoch',(c['ticker'],c['consensus_price'],'V402_CONSENSUS',now,time.time()))
def refresh_all(d,tickers,now=None):
 init(d);cs=[]
 for t in sorted(set(tickers)):c=consensus(t,now);store(d,c);cs.append(c)
 eligible=sum(c['execution_eligible'] for c in cs);covered=sum(c['status'] in ('PASS','VALUATION_ONLY') for c in cs);status='PASS' if len(cs)==covered==eligible else 'FAILED'
 d.execute('INSERT INTO quote_readiness(created,open_positions,covered,execution_eligible,status,detail_json) VALUES(?,?,?,?,?,?)',(dt.datetime.now(dt.timezone.utc).isoformat(),len(cs),covered,eligible,status,json.dumps(cs,sort_keys=True)));d.commit();return {'status':status,'open_positions':len(cs),'covered':covered,'execution_eligible':eligible,'quotes':cs}
