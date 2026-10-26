#!/usr/bin/env python3
import importlib.util,json,sqlite3,datetime as dt
P='/opt/paper-agent/app/research_v31.py';s=importlib.util.spec_from_file_location('r',P);r=importlib.util.module_from_spec(s);s.loader.exec_module(r);db=sqlite3.connect('/var/lib/paper-agent/paper-agent.db',timeout=60);db.row_factory=sqlite3.Row;db.execute('PRAGMA busy_timeout=60000');r.init(db)
for x in db.execute("SELECT p.decision_id,p.ticker,p.entry_price,p.opened FROM positions p WHERE p.status='OPEN'").fetchall():
 try:epoch=dt.datetime.fromisoformat(x['opened'].replace('Z','+00:00')).timestamp();print(x['ticker'],r.observe(db,x['decision_id'],x['ticker'],x['entry_price'],epoch))
 except Exception as e:print(x['ticker'],type(e).__name__,str(e)[:120])
print(json.dumps(r.report(db),indent=2));db.close()
