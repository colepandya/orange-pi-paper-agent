#!/usr/bin/env python3
"""V3.6.1 single-page hotfix: forecasts inline with decisions and positions."""
import html,importlib.util,json,os,sqlite3
BASE=os.getenv('PAPER_AGENT_BASE_DASHBOARD','/opt/paper-agent/app/dashboard_single_v352.py')
spec=importlib.util.spec_from_file_location('base352',BASE);base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
def esc(v):return html.escape(str(v if v is not None else 'Unavailable'))
def pct(v):return 'Unavailable' if v is None else f'{float(v):+,.2%}'
def money(v):return base.money(v)
def plain_event(v):return str(v or 'general_news').replace('_',' ').title()
def load_json(v,default):
 try:return json.loads(v) if isinstance(v,str) else (v or default)
 except Exception:return default
def add_trading_days(opened,days):
 x=base.utc(opened);left=int(days)
 while x and left:
  import datetime as dt;x+=dt.timedelta(days=1)
  if x.weekday()<5:left-=1
 return x.isoformat() if x else None
def enrich(data):
 d=sqlite3.connect(base.DB,timeout=10);d.row_factory=sqlite3.Row;d.execute('PRAGMA busy_timeout=10000')
 try:
  hasf=d.execute("SELECT 1 FROM sqlite_master WHERE name='event_impact_forecasts'").fetchone()
  hasp=d.execute("SELECT 1 FROM sqlite_master WHERE name='position_forecast_plans'").fetchone()
  forecasts={r['decision_id']:dict(r) for r in d.execute('SELECT * FROM event_impact_forecasts')} if hasf else {}
  plans={r['position_id']:dict(r) for r in d.execute('SELECT * FROM position_forecast_plans WHERE active=1')} if hasp else {}
  for x in data['decisions']:x['forecast']=forecasts.get(x['id'])
  monitor=d.execute("SELECT * FROM position_monitor_runs ORDER BY id DESC LIMIT 1").fetchone() if d.execute("SELECT 1 FROM sqlite_master WHERE name='position_monitor_runs'").fetchone() else None
  for x in data['positions']:
   p=plans.get(x['id']);f=forecasts.get(x['decision_id']);x['forecast_plan']=p;x['forecast']=f
   if p:
    x['target']=p['target_price'];x['stop']=p['stop_price'];x['days']=p['max_hold_days'];x['exit']=p['estimated_exit']
   elif f:
    x['target']=float(x['entry_price'])*(1+float(f['target_return']));x['stop']=float(x['entry_price'])*(1+float(f['stop_return']));x['days']=int(f['max_hold_days']);x['exit']=add_trading_days(x['opened'],x['days'])

  data['monitor_run']=dict(monitor) if monitor else None
  lifecycle={r['position_id']:dict(r) for r in d.execute("SELECT * FROM position_lifecycle")} if d.execute("SELECT 1 FROM sqlite_master WHERE name='position_lifecycle'").fetchone() else {}
  actions={r['position_id']:dict(r) for r in d.execute("SELECT a.* FROM virtual_exit_actions a JOIN (SELECT position_id,MAX(created) created FROM virtual_exit_actions GROUP BY position_id) x ON x.position_id=a.position_id AND x.created=a.created")} if d.execute("SELECT 1 FROM sqlite_master WHERE name='virtual_exit_actions'").fetchone() else {}
  reforecasts={r['position_id']:dict(r) for r in d.execute("SELECT t.* FROM target_reforecasts t JOIN (SELECT position_id,MAX(id) id FROM target_reforecasts GROUP BY position_id) x ON x.id=t.id")} if d.execute("SELECT 1 FROM sqlite_master WHERE name='target_reforecasts'").fetchone() else {}
  changes={r['position_id']:dict(r) for r in d.execute("SELECT c.* FROM position_plan_changes c JOIN (SELECT position_id,MAX(id) id FROM position_plan_changes GROUP BY position_id) x ON x.id=c.id")} if d.execute("SELECT 1 FROM sqlite_master WHERE name='position_plan_changes'").fetchone() else {}
  for x in data['positions']:
   x['latest_change']=changes.get(x['id']);x['lifecycle']=lifecycle.get(x['id']);x['latest_action']=actions.get(x['id']);x['latest_reforecast']=reforecasts.get(x['id'])
 finally:d.close()
 return data
def metric(label,value,cls=''):
 return f'<div class="fmetric"><span>{esc(label)}</span><strong class="{cls}">{esc(value)}</strong></div>'
def bullet(items):return '<ul>'+''.join(f'<li>{esc(x)}</li>' for x in items)+'</ul>'
def forecast_detail(f):
 if not f:return '<div class="forecast-empty">No V3.6 forecast is recorded for this decision.</div>'
 inputs=load_json(f.get('inputs_json'),{});deriv=load_json(f.get('derivation_json'),{});invalid=load_json(f.get('invalidation_json'),[]);analogue=inputs.get('analogue') or {};fund=inputs.get('fundamentals') or {}
 amount=inputs.get('amount');ratio=inputs.get('materiality_ratio');decision=f.get('decision') or 'UNKNOWN'
 overview=(f"The event model estimated a total impact of {pct(deriv.get('expected_total_impact'))}. "f"After subtracting {pct(f.get('priced_in'))} already reflected in price and applying uncertainty and cost adjustments, the estimated residual opportunity is {pct(f.get('residual_return'))}.")
 evidence=[]
 evidence.append(f"Event classification: {plain_event(f.get('event_type'))}")
 evidence.append(f"Disclosed event value: {money(amount) if amount else 'No reliable monetary value extracted'}")
 evidence.append(f"Materiality ratio: {pct(ratio) if ratio is not None else 'Not available'}")
 evidence.append(f"Comparable events: {int(analogue.get('n') or 0)}")
 evidence.append(f"Fundamental fields available: {len([v for v in fund.values() if v not in (None,'')])}")
 evidence.append(f"Estimated execution cost: {pct(inputs.get('estimated_cost'))}")
 return f'''<div class="forecast-inline"><div class="forecast-head"><div><b>Event-impact forecast</b><small>V{esc(f.get('forecast_version'))} · {esc(decision)} · calculated {base.nz(f.get('created'))}</small></div><span class="confidence">{float(f.get('confidence') or 0):.0f}% confidence</span></div><p class="forecast-summary">{esc(overview)}</p><div class="forecast-metrics">{metric('Bear case',pct(f.get('bear_return')),'neg')}{metric('Base case',pct(f.get('base_return')))}{metric('Bull case',pct(f.get('bull_return')),'pos')}{metric('Expected return',pct(f.get('expected_return')))}{metric('Forecast target',pct(f.get('target_return')),'pos')}{metric('Forecast stop',pct(f.get('stop_return')),'neg')}{metric('Maximum hold',f"{int(f.get('max_hold_days') or 0)} sessions")}{metric('Target probability',f"{float(f.get('target_probability') or 0):.1%}")}{metric('Proposed allocation',f"{float(f.get('allocation_pct') or 0):.1%}")}{metric('Data completeness',f"{float(f.get('data_completeness') or 0):.0f}%")}</div><div class="forecast-columns"><div><h4>Why this plan</h4>{bullet(evidence)}</div><div><h4>Exit structure</h4>{bullet([f"Sell {float(f.get('partial_exit_pct') or 1):.0%} at the first target",f"Runner target: {pct(f.get('runner_target'))}",f"Runner stop after partial exit: {pct(f.get('runner_stop'))}","The target is residual event impact after uncertainty and estimated costs, not a fixed percentage."])}</div><div><h4>Thesis invalidation</h4>{bullet(invalid)}</div></div><small class="hash">Forecast record {esc(str(f.get('forecast_hash') or '')[:16])}</small></div>'''
def positions_section(data):
 rows=''
 for x in data['positions']:
  p=x.get('forecast_plan');f=x.get('forecast');pc='pos' if (x.get('pnl') or 0)>=0 else 'neg';stale=' · STALE' if x.get('stale') else ''
  change=x.get('latest_change');life=x.get('lifecycle') or {};action=x.get('latest_action');rf=x.get('latest_reforecast');label=('V3.6.2 '+change['change_type'].lower()+' · '+base.nz(change['created']) if change else 'V3.6.2 monitored · no material change') if p else 'V3.6 forecast-derived display' if f else 'Legacy fixed rule'
  target_ret=p.get('target_return') if p else f.get('target_return') if f else None;stop_ret=p.get('stop_return') if p else f.get('stop_return') if f else None;prob=p.get('target_probability') if p else f.get('target_probability') if f else None;conf=p.get('confidence') if p else f.get('confidence') if f else None
  rows+=f'''<tr><td><b>{esc(x['ticker'])}</b><small>{esc(x['theme'])} · {plain_event(x.get('event_type'))}</small></td><td>{base.nz(x['opened'])}<small>Decision {base.nz(x['decision_time'])}</small></td><td>{money(x['entry_price'])}<small>{int(x['shares']):,} shares</small><div class="cost">{money(x['cost'])}</div><small>Total capital buy price</small></td><td>{money(x['current'])}<small>{esc(x['quote_source'])}{stale}<br>{base.nz(x['quote_at'])}</small></td><td>{money(x['market_value'])}</td><td><span class="pnl {pc}">{money(x['pnl'])}</span><small class="{pc}">{base.pct(x['pnl_pct'])}</small></td><td><b>{money(x['target'])}</b><small>{pct(target_ret)} forecast target</small><b class="neg">{money(x['stop'])}</b><small>{pct(stop_ret)} forecast stop</small></td><td>{base.nz(x['exit'])}<small>{int(x['days'])} trading sessions</small><small class="plan-label">{label}</small><small>Lifecycle {esc(life.get('phase','OPEN'))} · Realised {money(life.get('realized_pnl',0))}</small><small>{esc(('Rapid reforecast: '+rf['decision']) if rf else ('Last action: '+action['trigger']) if action else 'No exit action')}</small></td><td><span class="score s{90 if x['score']>=90 else 80 if x['score']>=80 else 60 if x['score']>=65 else 0}">{x['score']:.0f}</span><small>Target probability {float(prob or 0):.1%}<br>Forecast confidence {float(conf or 0):.0f}%</small></td></tr>'''
 if not rows:rows='<tr><td colspan="9" class="empty">No open virtual positions</td></tr>'
 return f'''<section id="positions" class="section"><div class="section-title"><h2>Open virtual positions</h2><small>V3.6 forecast target, stop and expected holding period applied to every open research lot</small></div><div class="plan-notice"><b>Research-plan update:</b> displayed exit levels now come from each position’s event-impact forecast. Plans are recorded and versioned but do not silently execute an exit.</div><div class="tablewrap"><table><thead><tr><th>Position</th><th>Opened NZT</th><th>Entry / capital buy</th><th>Latest quote</th><th>Market value</th><th>Unrealised P&amp;L</th><th>Forecast target / stop</th><th>Forecast exit NZT</th><th>Signal / confidence</th></tr></thead><tbody>{rows}</tbody></table></div></section>'''
def decisions_section(data,params):
 rows=''
 for x in data['decisions']:
  f=x.get('forecast');band=90 if float(x['score'] or 0)>=90 else 80 if float(x['score'] or 0)>=80 else 60 if float(x['score'] or 0)>=65 else 0
  summary=(f"{pct(f['target_return'])} target · {pct(f['stop_return'])} stop · {int(f['max_hold_days'])} sessions · {float(f['confidence']):.0f}% confidence" if f else 'Forecast unavailable')
  rows+=f'''<tr><td>{base.nz(x['created'])}</td><td><b>{esc(x['ticker'])}</b><small>{esc(x['theme'])}</small></td><td>{esc(x['action'])}</td><td><span class="score s{band}">{float(x['score'] or 0):.0f}</span></td><td><span class="tag">{esc(x['status'])}</span></td><td>{plain_event(x['event_type'])}</td><td>{esc(x['source'])}</td><td><b>{esc(summary)}</b><details><summary>Inspect evidence, decision logic and impact forecast</summary><div class="detail"><div class="mini"><b>Event evidence</b><strong>{esc(x['title'])}</strong><p>{esc(x['summary'])}</p><a href="{esc(x['url'])}" target="_blank">Open source</a><small>Published {base.nz(x['published'])}</small></div><div class="mini"><b>Recorded decision</b><p>{esc(x['reason'])}</p><small>Model {esc(x['model_version'])} · Rules {esc(x['rules_version'])}</small></div></div>{forecast_detail(f)}</details></td></tr>'''
 filters=f'''<form class="filters" method="get" action="#decisions"><input name="ticker" value="{esc(params['ticker'])}" placeholder="Ticker"><input name="source" value="{esc(params['source'])}" placeholder="Source"><select name="status"><option value="">All statuses</option>{''.join(f'<option value="{s}"'+(' selected' if params['status']==s else '')+f'>{s}</option>' for s in ['EXECUTED','REJECTED','BASELINE','AWAITING_PRICE','CAPACITY_DEFERRED','NO_PRICE','EXECUTION_ERROR'])}</select><input name="event" value="{esc(params['event'])}" placeholder="Event type"><select name="limit">{''.join(f'<option value="{n}"'+(' selected' if params['limit']==n else '')+f'>{n} decisions</option>' for n in [50,100,250,500])}</select><button>Apply filters</button><a href="/#decisions">Reset</a></form>'''
 return f'''<section id="decisions" class="section"><div class="section-title"><h2>Decision ledger</h2><small>{len(data['decisions'])} displayed · impact forecast included in every line item</small></div><div class="panel">{filters}</div><div class="tablewrap"><table><thead><tr><th>Decision NZT</th><th>Ticker</th><th>Action</th><th>Score</th><th>Status</th><th>Event</th><th>Source</th><th>Decision and forecast</th></tr></thead><tbody>{rows}</tbody></table></div></section>'''
def lifecycle_history():
 d=sqlite3.connect(base.DB,timeout=10);d.row_factory=sqlite3.Row
 try:
  if not d.execute("SELECT 1 FROM sqlite_master WHERE name='virtual_exit_actions'").fetchone():return ''
  run=d.execute('SELECT * FROM executor_runs ORDER BY id DESC LIMIT 1').fetchone() if d.execute("SELECT 1 FROM sqlite_master WHERE name='executor_runs'").fetchone() else None
  rows=d.execute('SELECT a.*,p.ticker FROM virtual_exit_actions a JOIN positions p ON p.id=a.position_id ORDER BY a.created DESC LIMIT 30').fetchall()
  body=''.join(f"<tr><td>{base.nz(r['created'])}</td><td><b>{esc(r['ticker'])}</b></td><td>{esc(r['trigger'])}</td><td>{esc(r['phase_before'])} → {esc(r['phase_after'])}</td><td>{float(r['shares_filled'] or 0):,.4f}</td><td>{money(r['fill_price'])}</td><td>{money(r['realized_pnl'])}</td><td>{esc(r['status'])}</td></tr>" for r in rows) or '<tr><td colspan="8" class="empty">No lifecycle actions yet.</td></tr>'
  status=(f"Last executor {base.nz(run['created'])} · {run['positions_checked']} checked · {run['actions_committed']} actions · {run['holds_extended']} rapid holds · {run['blocked_stale']} stale blocked" if run else 'Executor awaiting first cycle')
  return f'''<section id="lifecycle" class="section"><div class="section-title"><h2>Position lifecycle and virtual exits</h2><small>{esc(status)}</small></div><div class="tablewrap"><table><thead><tr><th>Time NZT</th><th>Ticker</th><th>Trigger</th><th>Phase</th><th>Shares</th><th>Fill</th><th>Realised P&amp;L</th><th>Status</th></tr></thead><tbody>{body}</tbody></table></div></section>'''
 finally:d.close()

def replace_section(page,section_id,new):
 start=page.find(f'<section id="{section_id}"')
 if start<0:raise ValueError('section not found: '+section_id)
 end=page.find('</section>',start)
 if end<0:raise ValueError('section end not found: '+section_id)
 return page[:start]+new+page[end+10:]
def render(data,params):
 data=enrich(data);page=base.render(data,params);page=replace_section(page,'positions',positions_section(data));page=replace_section(page,'decisions',decisions_section(data,params));page=page.replace('<section id="strategy"',lifecycle_history()+'<section id="strategy"',1);m=data.get('monitor_run');monitor_text=('Last position review '+base.nz(m['created'])+' · '+str(m['events_reviewed'])+' events checked · '+str(m['plans_changed'])+' plans changed' if m else 'Position monitor awaiting first cycle');clock_old='<div class="clock">NZ time<br>'+esc(data['generated'])+'</div>';clock_new='<div class="clock"><b>Last collector</b> '+esc(data['last'])+'<br><b>Next collector</b> '+esc(data['next'])+'<br><b>Dashboard refresh</b> every 60 seconds<br><b>Position review</b> '+esc(monitor_text)+'</div>';page=page.replace(clock_old,clock_new);page=page.replace('PAPER AGENT · VIRTUAL TRADER','PAPER AGENT V3.6.3 · VIRTUAL RESEARCH').replace('Trader command centre','Event-impact trader command centre')
 style='''<style>.forecast-inline{margin:8px 10px 14px;padding:13px;background:#081725;border:1px solid #284862;border-radius:9px}.forecast-head{display:flex;justify-content:space-between;gap:12px;align-items:start}.forecast-head small{display:block;color:var(--muted);margin-top:4px}.confidence,.plan-label{color:var(--gold);font-weight:800}.forecast-summary{line-height:1.55}.forecast-metrics{display:grid;grid-template-columns:repeat(5,minmax(110px,1fr));gap:7px}.fmetric{background:#102338;border:1px solid var(--line);border-radius:7px;padding:8px}.fmetric span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase}.fmetric strong{display:block;margin-top:4px}.forecast-columns{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:8px}.forecast-columns>div{background:#0b1c2c;border-radius:7px;padding:9px}.forecast-columns h4{margin:0 0 7px;color:var(--muted);text-transform:uppercase;font-size:10px}.forecast-columns ul{margin:0;padding-left:18px;line-height:1.55}.hash{display:block;color:var(--muted);margin-top:8px}.plan-notice{background:#172819;border:1px solid #335e39;color:#caefd2;padding:9px 12px;border-radius:8px;margin-bottom:8px}.forecast-empty{padding:12px;color:var(--muted)}@media(max-width:900px){.forecast-metrics{grid-template-columns:repeat(2,1fr)}.forecast-columns{grid-template-columns:1fr}}</style>'''
 return page.replace('</head>',style+'</head>')
class H(base.H):
 def do_GET(self):
  try:
   q=base.parse_qs(base.urlparse(self.path).query);raw=(q.get('limit') or ['100'])[0];limit=int(raw) if raw.isdigit() else 100;limit=max(50,min(500,limit));params=dict(limit=limit,ticker=(q.get('ticker') or [''])[0],status=(q.get('status') or [''])[0],source=(q.get('source') or [''])[0],event=(q.get('event') or [''])[0])
   if base.urlparse(self.path).path=='/health':return self.send(200,json.dumps({'ok':True,'version':'3.6.3','mode':'virtual-only','port':8088}),'application/json')
   return self.send(200,render(base.snapshot(limit=limit,ticker=params['ticker'],status=params['status'],source=params['source'],event=params['event']),params))
  except Exception as e:return self.send(500,'Dashboard error: '+base.esc(type(e).__name__+': '+str(e)))
if __name__=='__main__':base.ThreadingHTTPServer(('0.0.0.0',base.PORT),H).serve_forever()
