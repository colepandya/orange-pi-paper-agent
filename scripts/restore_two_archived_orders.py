#!/usr/bin/env python3
import glob,os,shutil,sqlite3,sys,datetime as dt
LIVE='/var/lib/paper-agent/paper-agent.db';paths=sorted(set(glob.glob('/var/backups/paper-agent/**/*.db',recursive=True)),key=os.path.getmtime,reverse=True)
def fetch(p):
 try:
  d=sqlite3.connect(f'file:{p}?mode=ro',uri=True);d.row_factory=sqlite3.Row
  rows=d.execute("SELECT p.*,d.created decision_created,d.ticker decision_ticker,d.action,d.score,d.reason,d.rules_version FROM positions p JOIN decisions d ON d.id=p.decision_id WHERE d.status='EXECUTED' AND p.status='OPEN' ORDER BY p.id").fetchall();d.close();return rows
 except sqlite3.Error:return []
chosen=None
for p in paths:
 rows=fetch(p)
 if len(rows)==2:chosen=(p,rows);break
if not chosen:raise SystemExit('FAIL: no archived database containing exactly two open executed orders was found; live DB unchanged')
p,rows=chosen;backup=f"/var/backups/paper-agent/before-two-order-restore-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db";shutil.copy2(LIVE,backup);db=sqlite3.connect(LIVE,timeout=60);db.row_factory=sqlite3.Row;db.execute('PRAGMA busy_timeout=60000');db.execute('BEGIN IMMEDIATE')
try:
 existing=db.execute("SELECT COUNT(*) FROM positions WHERE status='OPEN'").fetchone()[0]
 if existing:raise RuntimeError(f'live database already has {existing} open positions')
 cash=db.execute('SELECT balance FROM ledger ORDER BY id DESC LIMIT 1').fetchone()[0]
 for r in rows:
  cost=float(r['cost']);ticker=r['ticker'];cur=db.execute("INSERT INTO decisions(created,news_uid,ticker,action,score,reason,rules_version,status,entry_price,shares,amount,theme) VALUES(?,NULL,?,'BUY',?,?,?,'EXECUTED',?,?,?,?)",(r['decision_created'],ticker,r['score'],r['reason'],r['rules_version'],r['entry_price'],r['shares'],cost,r['theme']));cash=round(cash-cost,2);db.execute("INSERT INTO positions(ticker,theme,opened,entry_price,shares,cost,decision_id,status) VALUES(?,?,?,?,?,?,?,'OPEN')",(ticker,r['theme'],r['opened'],r['entry_price'],r['shares'],cost,cur.lastrowid));db.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,'RESTORED_ARCHIVED_BUY',?,?,?)",(r['opened'],-cost,f'{ticker}:{cur.lastrowid}:archive',cash))
 db.commit()
except Exception:db.rollback();db.close();raise
print('PASS: restored exact two archived orders from',p);print('Backup:',backup)
for x in db.execute("SELECT ticker,opened,entry_price,shares,cost,status FROM positions WHERE status='OPEN'"):print(dict(x))
print('Cash:',cash);db.close()
