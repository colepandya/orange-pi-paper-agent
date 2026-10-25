#!/usr/bin/env python3
import csv, io, json, os, re, shutil, sys, tempfile, urllib.request
from datetime import datetime, timezone

NASDAQ='https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt'
OTHER='https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt'
SEC='https://www.sec.gov/files/company_tickers.json'
UA='OrangePiPaperAgent/1.3 universe-refresh contact=local-admin'
EXCHANGE={'Q':'NASDAQ Global Select','G':'NASDAQ Global','S':'NASDAQ Capital','N':'NYSE','A':'NYSE American','P':'NYSE Arca','Z':'Cboe BZX','V':'IEX'}
EXCLUDE=re.compile(r'\b(warrant|warrants|unit|units|right|rights|preferred|preference|depositary shares each representing.*preferred|note|notes|bond|debenture|etn|closed-end fund|acquisition corp)\b',re.I)
EQUITY=re.compile(r'\b(common stock|common shares?|ordinary shares?|american depositary shares?|american depository shares?|ads\b|adr\b|class [a-z0-9-]+ shares?|shares of beneficial interest|limited partnership units|lp common units)\b',re.I)
SUFFIX=re.compile(r'\s+(common stock|common shares?|ordinary shares?|american depositary shares?.*|american depository shares?.*|ads.*|adr.*|class [a-z0-9-]+.*|new common stock|common units.*)$',re.I)
CORPWORDS=re.compile(r'\b(incorporated|inc|corporation|corp|company|co|limited|ltd|plc|holdings?|group|the|new)\b',re.I)

def fetch(url,timeout=25):
 req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'text/plain,application/json,*/*'})
 with urllib.request.urlopen(req,timeout=timeout) as r: return r.read(8_000_000)
def rows_pipe(data):
 text=data.decode('utf-8-sig','replace'); return list(csv.DictReader(io.StringIO(text),delimiter='|'))
def clean_name(s): return re.sub(r'\s+',' ',SUFFIX.sub('',s or '').replace('  ',' ')).strip(' .,-')
def norm(s): return re.sub(r'[^a-z0-9]+',' ',s.lower()).strip()
def useful_alias(s):
 n=norm(s); words=n.split()
 return len(n)>=5 and len(words)>=1 and n not in {'the company','common stock','common shares','ordinary shares'}
def eligible(name,etf,test,status='N'):
 if etf=='Y' or test=='Y' or status not in ('','N'): return False
 if EXCLUDE.search(name): return False
 return bool(EQUITY.search(name))
def parse(nasdaq_bytes,other_bytes,sec_bytes):
 sec_raw=json.loads(sec_bytes); sec_by_ticker={}
 for v in sec_raw.values(): sec_by_ticker[str(v.get('ticker','')).upper()]={'cik':int(v.get('cik_str',0)),'title':str(v.get('title','')).strip()}
 out={}
 for r in rows_pipe(nasdaq_bytes):
  sym=(r.get('Symbol') or '').strip().upper(); name=(r.get('Security Name') or '').strip()
  if not sym or sym.startswith('FILE CREATION') or not eligible(name,r.get('ETF',''),r.get('Test Issue',''),r.get('Financial Status','')): continue
  add(out,sec_by_ticker,sym,name,'NASDAQ')
 for r in rows_pipe(other_bytes):
  sym=(r.get('NASDAQ Symbol') or r.get('ACT Symbol') or '').strip().upper(); name=(r.get('Security Name') or '').strip(); exch=EXCHANGE.get((r.get('Exchange') or '').strip(),(r.get('Exchange') or '').strip())
  if not sym or sym.startswith('FILE CREATION') or not eligible(name,r.get('ETF',''),r.get('Test Issue','')): continue
  add(out,sec_by_ticker,sym,name,exch)
 return out
def add(out,sec,sym,listed_name,exchange):
 if any(c in sym for c in '$+^=#') or sym.endswith(('.W','.U','.R')): return
 si=sec.get(sym,{})
 legal=si.get('title') or clean_name(listed_name)
 aliases=[]
 for a in (clean_name(listed_name),legal):
  if useful_alias(a) and norm(a)!=norm(legal) and a not in aliases: aliases.append(a)
 stem=CORPWORDS.sub(' ',legal); stem=re.sub(r'\s+',' ',stem).strip()
 if useful_alias(stem) and len(stem)>=6 and stem not in aliases: aliases.append(stem)
 out[sym]={'name':legal,'sector':'Unclassified','theme':'Unclassified','aliases':aliases[:4],'exchange':exchange,'cik':si.get('cik'),'verified':True}
def atomic_write(path,obj):
 path=os.path.abspath(path); os.makedirs(os.path.dirname(path),exist_ok=True); fd,tmp=tempfile.mkstemp(prefix='.universe-',dir=os.path.dirname(path));
 try:
  with os.fdopen(fd,'w',encoding='utf-8') as f: json.dump(obj,f,indent=2,sort_keys=True); f.write('\n'); f.flush(); os.fsync(f.fileno())
  if os.path.exists(path): shutil.copy2(path,path+'.previous')
  os.replace(tmp,path)
 finally:
  if os.path.exists(tmp): os.unlink(tmp)
def main():
 dest=sys.argv[1] if len(sys.argv)>1 else '/etc/paper-agent/watchlist.json'
 universe=parse(fetch(NASDAQ),fetch(OTHER),fetch(SEC))
 if len(universe)<2000: raise SystemExit(f'FAIL: only {len(universe)} verified equities; refusing to replace universe')
 atomic_write(dest,universe)
 print(json.dumps({'status':'ok','verified_equities':len(universe),'destination':dest,'refreshed_utc':datetime.now(timezone.utc).isoformat()}))
if __name__=='__main__': main()
