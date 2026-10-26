#!/usr/bin/env python3
import datetime as dt,html,importlib.util,json,os,sqlite3,subprocess,sys,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs,urlparse
from zoneinfo import ZoneInfo
DB=os.getenv('PAPER_AGENT_DB','/var/lib/paper-agent/paper-agent.db');PORT=int(os.getenv('PAPER_AGENT_PORT','8088'));NZ=ZoneInfo('Pacific/Auckland');QUOTE_TTL=60
DEFAULT_RULE=(10,.15,.10)
def dbopen(path=DB):
 d=sqlite3.connect(path,timeout=10);d.row_factory=sqlite3.Row;d.execute('PRAGMA busy_timeout=10000');return d
def utc(v):
 if not v:return None
 try:
  x=dt.datetime.fromisoformat(str(v).replace('Z','+00:00'));return x if x.tzinfo else x.replace(tzinfo=dt.timezone.utc)
 except ValueError:return None
def nz(v):
 x=utc(v)
 if not x:return 'Unavailable'
 y=x.astimezone(NZ);return y.strftime('%d %b %Y, %-I:%M:%S %p %Z')
def money(v):return 'Unavailable' if v is None else f'${float(v):,.2f}'
def pct(v):return 'Unavailable' if v is None else f'{float(v):+,.2%}'
def event_target(opened,event_type,entry,rules):
 days,profit,stop=rules.get(event_type,DEFAULT_RULE);x=utc(opened) or dt.datetime.now(dt.timezone.utc);left=days
 while left:
  x+=dt.timedelta(days=1)
  if x.weekday()<5:left-=1
 return {'days':days,'profit':profit,'stop':stop,'target_price':entry*(1+profit),'stop_price':entry*(1-stop),'exit_at':x.isoformat()}
def timer_times():
 try:
  p=subprocess.run(['systemctl','show','paper-agent-broad.timer','-p','LastTriggerUSec','-p','NextElapseUSecRealtime','--no-pager'],capture_output=True,text=True,timeout=2)
  x=dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line);return nz(x.get('LastTriggerUSec')),nz(x.get('NextElapseUSecRealtime'))
 except Exception:return 'Unavailable','Unavailable'
def load_quote_module():
 p='/opt/paper-agent/app/experiment_execution.py' if os.path.exists('/opt/paper-agent/app/experiment_execution.py') else os.path.join(os.path.dirname(__file__),'experiment_execution.py');s=importlib.util.spec_from_file_location('_quotes',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
def quote(d,ticker,watch,clock=time.time):
 row=d.execute('SELECT price,source,quoted_at,updated_epoch FROM quote_cache WHERE ticker=?',(ticker,)).fetchone()
 if row and clock()-float(row['updated_epoch'])<=QUOTE_TTL:return {'price':row['price'],'source':row['source'],'quoted_at':row['quoted_at'],'stale':False}
 try:
  q=load_quote_module().resolve_quote(d,watch,ticker);q['stale']=False;return q
 except Exception:
  if row:return {'price':row['price'],'source':row['source']+' (cached)','quoted_at':row['quoted_at'],'stale':True}
  return {'price':None,'source':'UNAVAILABLE','quoted_at':None,'stale':True}
def schema_has(d,name):return d.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone() is not None
def snapshot(path=DB,refresh_quotes=True,decision_limit=100):
 decision_limit=max(50,min(500,int(decision_limit)))
 d=dbopen(path);watch={}
 try:watch=json.load(open('/etc/paper-agent/watchlist.json'))
 except Exception:pass
 rules={}
 if schema_has(d,'event_exit_rules'):
  rules={r['event_type']:(r['max_days'],r['profit_target'],r['stop_loss']) for r in d.execute('SELECT * FROM event_exit_rules')}
 q="""SELECT p.id,p.ticker,p.theme,p.opened,p.entry_price,p.shares,p.cost,p.decision_id,d.score,d.reason,d.created decision_time,n.source,n.title,n.url,t.event_type FROM positions p JOIN decisions d ON d.id=p.decision_id LEFT JOIN news n ON n.uid=d.news_uid LEFT JOIN theses t ON t.decision_id=d.id WHERE p.status='OPEN' ORDER BY p.opened DESC"""
 positions=[]
 for r in d.execute(q):
  x=dict(r);qq=quote(d,x['ticker'],watch) if refresh_quotes else {'price':x['entry_price'],'source':'TEST','quoted_at':x['opened'],'stale':False};target=event_target(x['opened'],x.get('event_type') or 'general_news',float(x['entry_price']),rules);current=qq.get('price');value=current*float(x['shares']) if current else None;pnl=value-float(x['cost']) if value is not None else None
  x.update(target);x.update({'current_price':current,'quote_source':qq.get('source'),'quote_at':qq.get('quoted_at'),'quote_stale':qq.get('stale'),'market_value':value,'unrealized_pnl':pnl,'unrealized_pct':pnl/float(x['cost']) if pnl is not None and x['cost'] else None});positions.append(x)
 cashrow=d.execute('SELECT balance FROM ledger ORDER BY id DESC LIMIT 1').fetchone();cash=float(cashrow[0]) if cashrow else 0;deployed=sum(float(x['cost']) for x in positions);market=sum(float(x['market_value'] or x['cost']) for x in positions);equity=cash+market
 recent=[dict(x) for x in d.execute("SELECT d.created,d.ticker,d.action,d.score,d.status,d.reason,n.source,n.title,n.url FROM decisions d LEFT JOIN news n ON n.uid=d.news_uid ORDER BY d.id DESC LIMIT ?",(decision_limit,))]
 last,next_=timer_times();integrity=d.execute('PRAGMA quick_check').fetchone()[0];d.close();return {'cash':cash,'deployed':deployed,'market_value':market,'equity':equity,'unrealized':market-deployed,'positions':positions,'recent':recent,'paused':os.path.exists('/var/lib/paper-agent/PAUSE_TRADES'),'last_refresh':last,'next_refresh':next_,'integrity':integrity,'generated':nz(dt.datetime.now(dt.timezone.utc).isoformat())}
def render(data,decision_limit=100):
 def cls(v):return 'pos' if v is not None and v>=0 else 'neg'
 cards=''.join(f'<div class="card"><span>{html.escape(k)}</span><strong class="{cls(v) if isinstance(v,(int,float)) and k in ("Unrealised P&L",) else ""}">{money(v) if isinstance(v,(int,float)) else html.escape(str(v))}</strong></div>' for k,v in [('Portfolio equity',data['equity']),('Cash',data['cash']),('Capital at cost',data['deployed']),('Market value',data['market_value']),('Unrealised P&L',data['unrealized']),('Mode','PAUSED' if data['paused'] else 'ACTIVE')])
 rows=''
 for x in data['positions']:
  source=f"<a href='{html.escape(x['url'] or '#')}' target='_blank' rel='noopener'>{html.escape(x['source'] or 'Unknown')}</a>" if x.get('url') else html.escape(x.get('source') or 'Unknown')
  rows+=f"<tr><td><b>{html.escape(x['ticker'])}</b><small>{html.escape(x.get('theme') or '')}</small></td><td>{nz(x['opened'])}<small>Decision: {nz(x['decision_time'])}</small></td><td>{money(x['entry_price'])}<small>{int(x['shares']):,} shares · {money(x['cost'])}</small></td><td>{money(x['current_price'])}<small>{html.escape(x['quote_source'] or '')} · {nz(x['quote_at'])}{' · STALE' if x['quote_stale'] else ''}</small></td><td class='{cls(x['unrealized_pnl'])}'><b>{money(x['unrealized_pnl'])}</b><small>{pct(x['unrealized_pct'])} · value {money(x['market_value'])}</small></td><td><b>{money(x['target_price'])}</b><small>Stop {money(x['stop_price'])}</small></td><td>{nz(x['exit_at'])}<small>{x['days']} trading days · {html.escape(x.get('event_type') or 'general_news')}</small></td><td><b>{x['score']:.0f}</b><small>{source}</small></td></tr>"
 if not rows:rows='<tr><td colspan="8" class="empty">No open virtual positions</td></tr>'
 decisions=''.join(f"<tr><td>{nz(x['created'])}</td><td><b>{html.escape(x['ticker'] or '—')}</b></td><td>{html.escape(x['action'])}</td><td><span class='score s{int(float(x['score'] or 0)//10)*10}'>{float(x['score'] or 0):.0f}</span></td><td><span class='badge status-{html.escape(x['status'].lower())}'>{html.escape(x['status'])}</span></td><td>{html.escape(x['source'] or 'Unknown')}</td><td>{html.escape((x['title'] or x['reason'] or '')[:180])}</td></tr>" for x in data['recent'])
 return f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60"><title>Paper Agent Trader Dashboard</title><style>
:root{{--bg:#07111f;--panel:#0d1b2a;--line:#20354d;--text:#e8f0f7;--muted:#8fa6bc;--accent:#3ca4ff;--pos:#38d996;--neg:#ff6673;--warn:#ffca5c}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,-apple-system,sans-serif}}main{{max-width:1600px;margin:auto;padding:22px}}header{{display:flex;justify-content:space-between;gap:16px;align-items:end;margin-bottom:18px}}h1{{margin:0;font-size:26px}}h2{{font-size:17px;margin:26px 0 10px}}small,.muted{{display:block;color:var(--muted);font-size:12px;margin-top:5px}}.status{{text-align:right}}.cards{{display:grid;grid-template-columns:repeat(6,minmax(140px,1fr));gap:10px}}.card{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}}.card span{{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}}.card strong{{display:block;font-size:20px;margin-top:7px}}.pos{{color:var(--pos)!important}}.neg{{color:var(--neg)!important}}.table{{overflow:auto;background:var(--panel);border:1px solid var(--line);border-radius:10px}}table{{border-collapse:collapse;width:100%;min-width:1100px}}th,td{{padding:12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}th{{color:var(--muted);font-size:11px;text-transform:uppercase;position:sticky;top:0;background:#102238}}tr:hover td{{background:#102238}}a{{color:var(--accent)}}.score{{display:inline-block;min-width:42px;text-align:center;padding:4px 7px;border-radius:6px;font-weight:800}}.s90{{background:#087f5b}}.s80{{background:#1864ab}}.s70,.s60{{background:#b7791f}}.s50{{background:#4b5563}}.s40,.s30,.s20,.s10,.s0{{background:#8b2635}}.status-executed{{color:#38d996}}.status-rejected,.status-execution_error{{color:#ff6673}}.status-baseline{{color:#8fa6bc}}.status-awaiting_price,.status-capacity_deferred{{color:#ffca5c}}.badge{{padding:3px 7px;border:1px solid var(--line);border-radius:999px;font-size:11px}}.empty{{text-align:center;color:var(--muted);padding:30px}}footer{{color:var(--muted);margin:20px 0}}@media(max-width:900px){{.cards{{grid-template-columns:repeat(2,1fr)}}header{{display:block}}.status{{text-align:left;margin-top:8px}}}} </style></head><body><main><header><div><h1>Virtual Trader Dashboard</h1><div class="muted">Research experiment only · No broker connection · Auto-refreshes every 60 seconds</div></div><div class="status"><b>DB {html.escape(data['integrity'])}</b><small>Last collector: {data['last_refresh']}<br>Next collector: {data['next_refresh']}<br>Page generated: {data['generated']}</small></div></header><section class="cards">{cards}</section><h2>Open virtual positions</h2><div class="table"><table><thead><tr><th>Position</th><th>Opened (NZ)</th><th>Entry</th><th>Live / latest quote</th><th>Unrealised P&L</th><th>Target / stop</th><th>Estimated exit (NZ)</th><th>Signal</th></tr></thead><tbody>{rows}</tbody></table></div><div class="decision-head"><h2>Recent decisions <small>Colour coded by score and status</small></h2><form method="get"><label for="limit">Show</label><select id="limit" name="limit" onchange="this.form.submit()">{''.join(f'<option value="{n}"'+(' selected' if n==decision_limit else '')+f'>{n} decisions</option>' for n in (50,100,250,500))}</select><noscript><button type="submit">Apply</button></noscript></form></div><div class="table"><table><thead><tr><th>Time (NZ)</th><th>Ticker</th><th>Action</th><th>Score</th><th>Status</th><th>Source</th><th>Event / rationale</th></tr></thead><tbody>{decisions}</tbody></table></div><footer>Target prices and estimated exit times are deterministic strategy outputs, not guarantees. Live quotes may be delayed; source and time are shown for every position.</footer></main></body></html>'''
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def send(self,code,body,ctype='text/html; charset=utf-8'):
  raw=body.encode();self.send_response(code);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(raw)
 def do_GET(self):
  try:
   if urlparse(self.path).path=='/health':
    x=snapshot(refresh_quotes=False);return self.send(200,json.dumps({'ok':True,'cash':x['cash'],'equity':x['equity'],'positions':len(x['positions']),'db':x['integrity']}),'application/json')
   q=parse_qs(urlparse(self.path).query);raw=(q.get('limit') or ['100'])[0]
   try: limit=int(raw)
   except ValueError: limit=100
   limit=max(50,min(500,limit));return self.send(200,render(snapshot(decision_limit=limit),limit))
  except Exception as e:return self.send(500,f'Internal dashboard error: {html.escape(type(e).__name__+": "+str(e))}')
def serve(port=PORT):ThreadingHTTPServer(('0.0.0.0',port),Handler).serve_forever()
if __name__=='__main__':serve()
