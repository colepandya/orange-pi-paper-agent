#!/usr/bin/env python3
"""V4 fail-closed controls shared by event, quote, risk and lifecycle code."""
import datetime as dt,email.utils,hashlib,json,math,re,unicodedata
VERSION='4.0.0'; ENTRY_QUOTE_MAX=60; EXIT_QUOTE_MAX=180
POS=('contract award','regulatory approval','raises guidance','beats estimates','buyback','record revenue')
NEG=('convertible offering','securities offering','dilution','recall','investigation','data breach','cuts guidance','default','sanction','tariff','war','armed conflict','contract termination')
def norm(v):return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9$%.+-]+',' ',unicodedata.normalize('NFKC',v or '').casefold())).strip()
def phrase(text,p):return re.search(r'(?<![a-z0-9])'+re.escape(norm(p))+r'(?![a-z0-9])',norm(text)) is not None
def direction(text):
 p=sum(phrase(text,x) for x in POS);n=sum(phrase(text,x) for x in NEG)
 if phrase(text,'did not win') or phrase(text,'failed to win'):p=max(0,p-1);n+=1
 return 0 if p+n==0 else (p-n)/(p+n)
def parse_time(v):
 if not v:return None
 try:x=email.utils.parsedate_to_datetime(str(v))
 except (ValueError,TypeError,OverflowError):x=None
 if x is None:
  try:x=dt.datetime.fromisoformat(str(v).replace('Z','+00:00'))
  except (ValueError,TypeError,OverflowError):return None
 if x.tzinfo is None:return None
 return x.astimezone(dt.timezone.utc)
def freshness(v,now=None,max_seconds=172800):
 x=parse_time(v);now=now or dt.datetime.now(dt.timezone.utc)
 if x is None:return False,'INVALID_TIMESTAMP',None
 age=(now-x).total_seconds()
 if age < -300:return False,'FUTURE_TIMESTAMP',age
 if age > max_seconds:return False,'STALE',age
 return True,'PASS',age
def tighten_stop(old,direction_value):
 old=float(old)
 if direction_value>=-.1:return old
 return max(-.08,min(-.012,old*(1-.35*min(1,abs(direction_value)))))
def quote_valid(q,ticker,now=None,max_age=180):
 if not q:return False,'MISSING'
 if q.get('ticker') and q['ticker']!=ticker:return False,'IDENTITY_MISMATCH'
 p=q.get('price')
 if not isinstance(p,(int,float)) or not math.isfinite(p) or p<=0:return False,'INVALID_PRICE'
 ok,why,age=freshness(q.get('quoted_at'),now,max_age)
 return (ok,why) if ok else (False,why)
def forecast_gate(f,portfolio=None,limits=None):
 portfolio=portfolio or {};limits=limits or {'min_confidence':55,'min_completeness':40,'min_probability':.55,'min_edge':.01,'max_order':.05,'max_issuer':.10,'max_theme':.25,'max_gross':.60,'drawdown_pause':.05,'daily_loss':.01}
 reasons=[]
 if not f or f.get('decision')!='BUY':reasons.append('FORECAST_NOT_BUY')
 for k,m in [('confidence',limits['min_confidence']),('data_completeness',limits['min_completeness']),('target_probability',limits['min_probability']),('target_return',limits['min_edge'])]:
  if not f or f.get(k) is None or float(f[k])<m:reasons.append('LOW_'+k.upper())
 if portfolio.get('drawdown',0)<=-limits['drawdown_pause']:reasons.append('DRAWDOWN_PAUSE')
 if portfolio.get('daily_return',0)<=-limits['daily_loss']:reasons.append('DAILY_LOSS_PAUSE')
 proposed=min(float((f or {}).get('allocation_pct') or 0),limits['max_order'])
 if portfolio.get('issuer_weight',0)+proposed>limits['max_issuer']:reasons.append('ISSUER_LIMIT')
 if portfolio.get('theme_weight',0)+proposed>limits['max_theme']:reasons.append('THEME_LIMIT')
 if portfolio.get('gross_weight',0)+proposed>limits['max_gross']:reasons.append('GROSS_LIMIT')
 return ('REJECT',0,reasons) if reasons else ('APPROVE',proposed,[])
def story_hash(title,summary):return hashlib.sha256(' '.join(sorted(set(norm(title+' '+summary).split()))).encode()).hexdigest()
