#!/usr/bin/env python3
import datetime as dt,hashlib,json,os,sqlite3,subprocess
VERSION='3.5.0';DB='/var/lib/paper-agent/paper-agent.db'
SCHEMA='''
CREATE TABLE IF NOT EXISTS strategy_versions(version TEXT PRIMARY KEY,created TEXT NOT NULL,git_commit TEXT NOT NULL,config_hash TEXT NOT NULL,universe_hash TEXT NOT NULL,logic_json TEXT NOT NULL,logic_hash TEXT NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_lineage(decision_id INTEGER PRIMARY KEY,lineage_id TEXT UNIQUE NOT NULL,raw_event_hash TEXT,feature_json TEXT NOT NULL,feature_hash TEXT NOT NULL,logic_json TEXT NOT NULL,logic_hash TEXT NOT NULL,risk_json TEXT NOT NULL,risk_hash TEXT NOT NULL,strategy_version TEXT NOT NULL,git_commit TEXT NOT NULL,config_hash TEXT NOT NULL,universe_hash TEXT NOT NULL,captured_at TEXT NOT NULL,replay_status TEXT NOT NULL DEFAULT 'PENDING');
CREATE TABLE IF NOT EXISTS quote_observations(id INTEGER PRIMARY KEY AUTOINCREMENT,decision_id INTEGER,ticker TEXT NOT NULL,price REAL NOT NULL,source TEXT NOT NULL,quoted_at TEXT NOT NULL,observed_at TEXT NOT NULL,purpose TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS replay_results(id INTEGER PRIMARY KEY AUTOINCREMENT,decision_id INTEGER NOT NULL,replayed_at TEXT NOT NULL,expected_action TEXT,actual_action TEXT,expected_score REAL,actual_score REAL,matched INTEGER NOT NULL,detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS data_quality_events(id INTEGER PRIMARY KEY AUTOINCREMENT,created TEXT NOT NULL,severity TEXT NOT NULL,code TEXT NOT NULL,entity_type TEXT,entity_id TEXT,detail TEXT NOT NULL,resolved_at TEXT);
CREATE TABLE IF NOT EXISTS deployment_history(id INTEGER PRIMARY KEY AUTOINCREMENT,created TEXT NOT NULL,version TEXT NOT NULL,git_commit TEXT NOT NULL,host TEXT NOT NULL,status TEXT NOT NULL,backup_path TEXT,validation_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS market_readiness(id INTEGER PRIMARY KEY AUTOINCREMENT,checked_at TEXT NOT NULL,status TEXT NOT NULL,checks_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_lineage_version ON decision_lineage(strategy_version,captured_at);
CREATE INDEX IF NOT EXISTS idx_quality_open ON data_quality_events(severity,resolved_at);
'''
def now():return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
def sha(x):return hashlib.sha256(x if isinstance(x,bytes) else str(x).encode()).hexdigest()
def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'))
def filehash(path):
 try:return sha(open(path,'rb').read())
 except OSError:return 'UNAVAILABLE'
def git_commit():
 try:return subprocess.check_output(['git','-C','/home/orangepi/orange-pi-paper-agent','rev-parse','HEAD'],text=True,timeout=3).strip()
 except Exception:return 'UNAVAILABLE'
def connect(path=DB):
 d=sqlite3.connect(path,timeout=60);d.row_factory=sqlite3.Row;d.execute('PRAGMA busy_timeout=60000');return d
def init(d):d.executescript(SCHEMA);d.commit()
def logic_snapshot(d):
 rules=[dict(x) for x in d.execute('SELECT * FROM event_exit_rules ORDER BY event_type')] if d.execute("SELECT 1 FROM sqlite_master WHERE name='event_exit_rules'").fetchone() else []
 return {'entry_score':65,'allocation_ladder':[.10,.20,.35,.50],'max_single':.50,'max_theme':.80,'max_deployed':.90,'stale_hours':72,'exit_rules':rules,'virtual_only':True}
def register_strategy(d,version=VERSION):
 init(d);logic=logic_snapshot(d);lj=canonical(logic);gc=git_commit();ch=filehash('/etc/paper-agent/config.json');uh=filehash('/etc/paper-agent/watchlist.json');d.execute('INSERT OR IGNORE INTO strategy_versions VALUES(?,?,?,?,?,?,?,?)',(version,now(),gc,ch,uh,lj,sha(lj),'CHAMPION'));d.commit();return version
def capture_missing(d,limit=1000):
 init(d);version=register_strategy(d);logic=logic_snapshot(d);lj=canonical(logic);gc=git_commit();ch=filehash('/etc/paper-agent/config.json');uh=filehash('/etc/paper-agent/watchlist.json');rows=d.execute('''SELECT x.*,n.source,n.title,n.summary,n.url,n.published,t.event_type,t.thesis_json FROM decisions x LEFT JOIN news n ON n.uid=x.news_uid LEFT JOIN theses t ON t.decision_id=x.id LEFT JOIN decision_lineage l ON l.decision_id=x.id WHERE l.decision_id IS NULL ORDER BY x.id LIMIT ?''',(limit,)).fetchall();n=0
 for r in rows:
  z=dict(r);feature={'ticker':z.get('ticker'),'score':z.get('score'),'event_type':z.get('event_type'),'source':z.get('source'),'published':z.get('published'),'thesis':z.get('thesis_json'),'title':z.get('title')};risk={'status':z.get('status'),'action':z.get('action'),'amount':z.get('amount'),'reason':z.get('reason')};fj,rj=canonical(feature),canonical(risk);raw=sha(canonical({'source':z.get('source'),'title':z.get('title'),'summary':z.get('summary'),'url':z.get('url')}));lineage=sha(f"{r['id']}|{raw}|{version}");d.execute('INSERT INTO decision_lineage VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(r['id'],lineage,raw,fj,sha(fj),lj,sha(lj),rj,sha(rj),version,gc,ch,uh,now(),'PENDING'));n+=1
 d.commit();return n
def readiness(d):
 init(d);checks={};checks['integrity']=d.execute('PRAGMA quick_check').fetchone()[0];checks['strategy_registered']=bool(d.execute("SELECT 1 FROM strategy_versions WHERE status='CHAMPION'").fetchone());checks['unlineaged']=d.execute('SELECT COUNT(*) FROM decisions x LEFT JOIN decision_lineage l ON l.decision_id=x.id WHERE l.decision_id IS NULL').fetchone()[0];checks['negative_cash']=float(d.execute('SELECT balance FROM ledger ORDER BY id DESC LIMIT 1').fetchone()[0])<0;base=os.path.dirname(d.execute('PRAGMA database_list').fetchone()[2]) or '/tmp';checks['disk_free_mb']=os.statvfs(base).f_bavail*os.statvfs(base).f_frsize//1048576;critical=checks['integrity']!='ok' or not checks['strategy_registered'] or checks['unlineaged']>0 or checks['negative_cash'] or checks['disk_free_mb']<250;status='FAIL' if critical else 'PASS';d.execute('INSERT INTO market_readiness(checked_at,status,checks_json) VALUES(?,?,?)',(now(),status,canonical(checks)));d.commit();pause=os.path.join(base,'PAUSE_TRADES')
 if critical:
  with open(pause, 'w', encoding='utf-8') as handle:
   handle.write(canonical(checks) + '\n')
 return {'status':status,'checks':checks}
