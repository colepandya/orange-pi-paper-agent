#!/usr/bin/env python3
import datetime as dt,json,math,os,sqlite3,time,urllib.parse,urllib.request
try:
 from .market_clock_v401 import state
except ImportError:
 from market_clock_v401 import state
DB=os.getenv("PAPER_AGENT_DB","/var/lib/paper-agent/paper-agent.db")
def connect(path=DB):
 d=sqlite3.connect(path,timeout=60);d.row_factory=sqlite3.Row;d.execute("PRAGMA busy_timeout=60000");return d
def init(d):
 d.executescript("""CREATE TABLE IF NOT EXISTS market_observations(id INTEGER PRIMARY KEY, ticker TEXT,returned_ticker TEXT,session TEXT,price REAL,bid REAL,ask REAL,provider_timestamp TEXT,received_timestamp TEXT,source TEXT,quality_state TEXT,identity_verified INTEGER,execution_eligible INTEGER,failure_reason TEXT);CREATE INDEX IF NOT EXISTS ix_market_obs ON market_observations(ticker,id DESC);CREATE TABLE IF NOT EXISTS quote_refresh_runs(id INTEGER PRIMARY KEY,created TEXT,session TEXT,tickers INTEGER,current_quotes INTEGER,execution_eligible INTEGER,failures INTEGER,detail_json TEXT);""");d.commit()
def fetch(ticker,now=None):
 now=now or dt.datetime.now(dt.timezone.utc);clk=state(now);url="https://query1.finance.yahoo.com/v7/finance/quote?symbols="+urllib.parse.quote(ticker)
 try:j=json.load(urllib.request.urlopen(urllib.request.Request(url,headers={"User-Agent":"PaperAgent/4.0.1 virtual-research"}),timeout=12));q=j["quoteResponse"]["result"][0]
 except Exception as e:return {"ticker":ticker,"session":clk["session"],"quality_state":"FAILED","source":"YAHOO_QUOTE","failure_reason":type(e).__name__,"received_timestamp":now.isoformat(),"identity_verified":0,"execution_eligible":0}
 f={"PRE_MARKET":("preMarketPrice","preMarketTime"),"AFTER_HOURS":("postMarketPrice","postMarketTime"),"REGULAR":("regularMarketPrice","regularMarketTime")};pf,tf=f.get(clk["session"],("regularMarketPrice","regularMarketTime"));p=q.get(pf);ts=q.get(tf);ret=q.get("symbol");qt=dt.datetime.fromtimestamp(ts,dt.timezone.utc) if ts else None;age=(now-qt).total_seconds() if qt else None
 ident=ret==ticker;valid=isinstance(p,(int,float)) and math.isfinite(p) and p>0;bid=q.get("bid");ask=q.get("ask");spread=not(bid and ask) or bid<=ask and (ask-bid)/p<=.02;current=qt is not None and (abs(age)<=90 if clk["session"]!="CLOSED" else True);quality=("LIVE" if clk["session"]=="REGULAR" else "EXTENDED_HOURS") if current and clk["session"]!="CLOSED" else ("MARKET_CLOSED" if current else "STALE");eligible=int(clk["execution"] and ident and valid and spread and current)
 reason=None if eligible or quality=="MARKET_CLOSED" else ("IDENTITY_MISMATCH" if not ident else "INVALID_PRICE" if not valid else "INVALID_SPREAD" if not spread else "STALE_FOR_SESSION")
 return {"ticker":ticker,"returned_ticker":ret,"session":clk["session"],"price":p,"bid":bid,"ask":ask,"provider_timestamp":qt.isoformat() if qt else None,"received_timestamp":now.isoformat(),"source":"YAHOO_QUOTE","quality_state":quality,"identity_verified":int(ident),"execution_eligible":eligible,"failure_reason":reason}
def refresh(d,tickers,now=None):
 init(d);a=[]
 for t in sorted(set(tickers)):
  x=fetch(t,now);a.append(x);d.execute("INSERT INTO market_observations(ticker,returned_ticker,session,price,bid,ask,provider_timestamp,received_timestamp,source,quality_state,identity_verified,execution_eligible,failure_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",tuple(x.get(k) for k in ("ticker","returned_ticker","session","price","bid","ask","provider_timestamp","received_timestamp","source","quality_state","identity_verified","execution_eligible","failure_reason")))
  if x.get("price") and x.get("provider_timestamp"):d.execute("INSERT INTO quote_cache(ticker,price,source,quoted_at,updated_epoch) VALUES(?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET price=excluded.price,source=excluded.source,quoted_at=excluded.quoted_at,updated_epoch=excluded.updated_epoch",(t,x["price"],x["source"],x["provider_timestamp"],time.time()))
 clk=state(now);d.execute("INSERT INTO quote_refresh_runs(created,session,tickers,current_quotes,execution_eligible,failures,detail_json) VALUES(?,?,?,?,?,?,?)",(dt.datetime.now(dt.timezone.utc).isoformat(),clk["session"],len(a),sum(x["quality_state"] in ("LIVE","EXTENDED_HOURS","MARKET_CLOSED") for x in a),sum(x["execution_eligible"] for x in a),sum(bool(x.get("failure_reason")) for x in a),json.dumps(a)));d.commit();return a
def tickers(d):return [r[0] for r in d.execute("SELECT DISTINCT ticker FROM positions WHERE status IN ('OPEN','RUNNER_OPEN')")]
def force(d,ticker,now=None,execution=True):
 x=fetch(ticker,now);refresh(d,[ticker],now);return x if not execution or x.get("execution_eligible") else None
if __name__=="__main__":d=connect();print(json.dumps(refresh(d,tickers(d)),indent=2));d.close()
