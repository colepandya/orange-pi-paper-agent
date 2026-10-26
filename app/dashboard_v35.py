#!/usr/bin/env python3
import html,json,os,sqlite3
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs,urlparse
DB='/var/lib/paper-agent/paper-agent.db';PORT=8088
CSS='''<style>:root{--bg:#07101d;--panel:#0d1928;--line:#20344b;--txt:#e7eef6;--muted:#8da5ba;--blue:#39a0ff;--green:#2dd48f;--red:#ff6673;--amber:#ffc857}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:13px system-ui}nav{position:sticky;top:0;display:flex;align-items:center;gap:8px;padding:12px 20px;background:#081522;border-bottom:1px solid var(--line);z-index:9}nav b{margin-right:18px}nav a{color:var(--muted);text-decoration:none;padding:8px 10px;border-radius:7px}nav a:hover{background:#12263b;color:white}main{max-width:1700px;margin:auto;padding:20px}.grid{display:grid;grid-template-columns:repeat(5,minmax(130px,1fr));gap:10px}.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px}.card span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase}.card strong{display:block;font-size:20px;margin-top:6px}.toolbar{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.toolbar input,.toolbar select,.toolbar button{background:#102238;color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:8px}.table{overflow:auto;border:1px solid var(--line);border-radius:10px}table{border-collapse:collapse;width:100%;min-width:1100px;background:var(--panel)}th,td{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{position:sticky;top:54px;background:#102238;color:var(--muted);font-size:11px;text-transform:uppercase}a{color:var(--blue)}small{display:block;color:var(--muted);margin-top:4px}.score{font-weight:800;padding:4px 8px;border-radius:6px;display:inline-block}.hi{background:#087f5b}.good{background:#1864ab}.mid{background:#9c6b13}.low{background:#792936}.ok{color:var(--green)}.bad{color:var(--red)}pre{white-space:pre-wrap;word-break:break-word;background:#07101d;padding:12px;border-radius:8px}.twocol{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:850px){.grid{grid-template-columns:repeat(2,1fr)}.twocol{grid-template-columns:1fr}}</style>'''
def db():
 d=sqlite3.connect(DB,timeout=10);d.row_factory=sqlite3.Row;d.execute('PRAGMA busy_timeout=10000');return d
def nav():return '<nav><b>Paper Agent V3.5</b><a href="/">Overview</a><a href="/decisions">Decisions</a><a href="/strategy">Strategy</a><a href="/learning">Learning</a><a href="/operations">Operations</a></nav>'
def page(title,body):return f'<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>{CSS}{nav()}<main>{body}</main>'
def esc(x):return html.escape(str(x if x is not None else '—'))
def scoreclass(x):return 'hi' if x>=90 else 'good' if x>=80 else 'mid' if x>=65 else 'low'
def cards(items):return '<div class=grid>'+''.join(f'<div class=card><span>{esc(k)}</span><strong>{esc(v)}</strong></div>' for k,v in items)+'</div>'
def overview(d):
 cash=d.execute('select balance from ledger order by id desc limit 1').fetchone()[0];pos=d.execute("select count(*),coalesce(sum(cost),0) from positions where status='OPEN'").fetchone();dec=d.execute('select count(*) from decisions').fetchone()[0];lin=d.execute('select count(*) from decision_lineage').fetchone()[0] if d.execute("select 1 from sqlite_master where name='decision_lineage'").fetchone() else 0;ready=d.execute('select status,checked_at from market_readiness order by id desc limit 1').fetchone() if d.execute("select 1 from sqlite_master where name='market_readiness'").fetchone() else None
 return page('Overview','<h1>Command centre</h1>'+cards([('Cash',f'${cash:,.2f}'),('Open positions',pos[0]),('Capital at cost',f'${pos[1]:,.2f}'),('Decisions',dec),('Lineage captured',f'{lin}/{dec}')])+f'<div class=panel><h2>Market readiness</h2><b class={"ok" if ready and ready[0]=="PASS" else "bad"}>{esc(ready[0] if ready else "NOT RUN")}</b><small>{esc(ready[1] if ready else "Run readiness job")}</small></div>')
def decisions(d,q):
 limit=max(50,min(500,int((q.get('limit') or ['100'])[0] if (q.get('limit') or ['100'])[0].isdigit() else 100)));ticker=(q.get('ticker') or [''])[0].upper();status=(q.get('status') or [''])[0];p=['1=1'];a=[]
 if ticker:p+=['x.ticker like ?'];a+=[f'%{ticker}%']
 if status:p+=['x.status=?'];a+=[status]
 a+=[limit];rows=d.execute(f'''select x.id,x.created,x.ticker,x.action,x.score,x.status,n.source,t.event_type,l.lineage_id,l.strategy_version from decisions x left join news n on n.uid=x.news_uid left join theses t on t.decision_id=x.id left join decision_lineage l on l.decision_id=x.id where {' and '.join(p)} order by x.id desc limit ?''',a).fetchall();body='<h1>Decision ledger</h1><form class=toolbar><input name=ticker value="'+esc(ticker)+'" placeholder="Ticker"><select name=status><option value="">All statuses</option>'+''.join(f'<option{(" selected" if status==x else "")}>{x}</option>' for x in ['EXECUTED','REJECTED','BASELINE','AWAITING_PRICE','CAPACITY_DEFERRED','NO_PRICE','EXECUTION_ERROR'])+'</select><select name=limit>'+''.join(f'<option value={x}{(" selected" if x==limit else "")}>{x} decisions</option>' for x in [50,100,250,500])+'</select><button>Apply</button></form><div class=table><table><tr><th>Time</th><th>Ticker</th><th>Action</th><th>Score</th><th>Status</th><th>Event</th><th>Source</th><th>Lineage</th></tr>'
 for row in rows:
  score = float(row[4] or 0)
  lineage = (row[8] or "MISSING")[:16]

  body += (
   f'<tr>'
   f'<td>{esc(row[1])}</td>'
   f'<td><a href="/decision?id={row[0]}">'
   f'<b>{esc(row[2])}</b></a></td>'
   f'<td>{esc(row[3])}</td>'
   f'<td><span class="score {scoreclass(score)}">'
   f'{score:.0f}</span></td>'
   f'<td>{esc(row[5])}</td>'
   f'<td>{esc(row[7])}</td>'
   f'<td>{esc(row[6])}</td>'
   f'<td><small>{esc(lineage)}</small></td>'
   f'</tr>'
  )
 return page('Decisions',body+'</table></div>')
def detail(d,q):
 try:i=int((q.get('id') or ['0'])[0])
 except:i=0
 r=d.execute('''select x.*,n.source,n.title,n.summary,n.url,n.published,t.*,l.* from decisions x left join news n on n.uid=x.news_uid left join theses t on t.decision_id=x.id left join decision_lineage l on l.decision_id=x.id where x.id=?''',(i,)).fetchone()
 if not r:return page('Not found','<h1>Decision not found</h1>')
 z=dict(r);return page(f'Decision {i}',f'<h1>{esc(z.get("ticker"))} · Decision {i}</h1>'+cards([('Action',z.get('action')),('Score',z.get('score')),('Status',z.get('status')),('Event',z.get('event_type')),('Model',z.get('strategy_version'))])+f'<div class=twocol><div class=panel><h2>Evidence</h2><b>{esc(z.get("title"))}</b><p>{esc(z.get("summary"))}</p><a href="{esc(z.get("url"))}">{esc(z.get("source"))}</a><h3>Timestamps</h3><pre>{esc(canonical({k:z.get(k) for k in ["published","created","captured_at"]}))}</pre></div><div class=panel><h2>Decision and risk</h2><pre>{esc(z.get("reason"))}</pre><h3>Frozen features</h3><pre>{esc(z.get("feature_json"))}</pre><h3>Frozen logic</h3><pre>{esc(z.get("logic_json"))}</pre><h3>Risk snapshot</h3><pre>{esc(z.get("risk_json"))}</pre></div></div>')
def canonical(x):return json.dumps(x,indent=2,default=str)
def strategy(d):
 rows=d.execute('select * from strategy_versions order by created desc').fetchall();return page('Strategy','<h1>Strategy registry</h1>'+''.join(f'<div class=panel><h2>{esc(r[0])} <span class=ok>{esc(r[7])}</span></h2><small>Git {esc(r[2])} · logic {esc(r[6])}</small><pre>{esc(r[5])}</pre></div>' for r in rows))
def learning(d):
 c=d.execute('select * from challenger_results order by created desc limit 50').fetchall() if d.execute("select 1 from sqlite_master where name='challenger_results'").fetchone() else [];r=d.execute('select * from strategy_recommendations order by created desc limit 20').fetchall() if d.execute("select 1 from sqlite_master where name='strategy_recommendations'").fetchone() else [];return page('Learning','<h1>Champion and challengers</h1><div class=twocol><div class=panel><h2>Recent challenger results</h2><pre>'+esc(canonical([dict(x) for x in c]))+'</pre></div><div class=panel><h2>Recommendations requiring approval</h2><pre>'+esc(canonical([dict(x) for x in r]))+'</pre></div></div>')
def operations(d):
 rec=d.execute('select * from daily_reconciliation order by created desc limit 20').fetchall() if d.execute("select 1 from sqlite_master where name='daily_reconciliation'").fetchone() else [];ready=d.execute('select * from market_readiness order by id desc limit 20').fetchall() if d.execute("select 1 from sqlite_master where name='market_readiness'").fetchone() else [];q=d.execute('select * from data_quality_events where resolved_at is null order by id desc limit 100').fetchall() if d.execute("select 1 from sqlite_master where name='data_quality_events'").fetchone() else [];return page('Operations','<h1>Operations and controls</h1><div class=twocol><div class=panel><h2>Readiness</h2><pre>'+esc(canonical([dict(x) for x in ready]))+'</pre><h2>Reconciliation</h2><pre>'+esc(canonical([dict(x) for x in rec]))+'</pre></div><div class=panel><h2>Open data-quality events</h2><pre>'+esc(canonical([dict(x) for x in q]))+'</pre></div></div>')
class H(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_GET(self):
  u=urlparse(self.path);q=parse_qs(u.query);d=db()
  try:
   if u.path=='/health':out=json.dumps({'ok':True,'version':'3.5.0'});ct='application/json'
   elif u.path=='/decisions':out=decisions(d,q);ct='text/html'
   elif u.path=='/decision':out=detail(d,q);ct='text/html'
   elif u.path=='/strategy':out=strategy(d);ct='text/html'
   elif u.path=='/learning':out=learning(d);ct='text/html'
   elif u.path=='/operations':out=operations(d);ct='text/html'
   else:out=overview(d);ct='text/html'
  finally:d.close()
  b=out.encode();self.send_response(200);self.send_header('Content-Type',ct);self.send_header('Content-Length',str(len(b)));self.end_headers();self.wfile.write(b)
if __name__=='__main__':ThreadingHTTPServer(('0.0.0.0',PORT),H).serve_forever()
