"""SQLite access + schema + non-destructive migration.

* Legacy tables from v1-v4 are created ``IF NOT EXISTS`` with their original definitions, so an existing
  production database (ledger, positions, decisions, plans) keeps working untouched.
* The legacy per-decision ``theses`` table collides with the OMEGA ``theses`` table, so it is renamed to
  ``decision_theses`` (data preserved) before the new schema is applied.
* New columns are added with guarded ALTER TABLE statements.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from typing import Dict, Iterator, List, Optional, Tuple

from .timeutil import iso, utcnow

SCHEMA_VERSION = 6

LEGACY = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS news(id INTEGER PRIMARY KEY,uid TEXT UNIQUE,source TEXT,title TEXT,summary TEXT,url TEXT,published TEXT,ingested TEXT,trust REAL);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY,created TEXT,news_uid TEXT,ticker TEXT,action TEXT,score REAL,reason TEXT,rules_version TEXT,status TEXT,entry_price REAL,shares INTEGER,amount REAL,theme TEXT,FOREIGN KEY(news_uid) REFERENCES news(uid));
CREATE TABLE IF NOT EXISTS positions(id INTEGER PRIMARY KEY,ticker TEXT,theme TEXT,opened TEXT,entry_price REAL,shares INTEGER,cost REAL,decision_id INTEGER,status TEXT,closed TEXT,exit_price REAL,pnl REAL,review_code TEXT,FOREIGN KEY(decision_id) REFERENCES decisions(id));
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY,created TEXT,kind TEXT,amount REAL,ref TEXT,balance REAL);
CREATE TABLE IF NOT EXISTS daily_values(day TEXT PRIMARY KEY,equity REAL,cash REAL,deployed REAL,drawdown REAL);
CREATE TABLE IF NOT EXISTS lessons(id INTEGER PRIMARY KEY,created TEXT,category TEXT,ticker TEXT,detail TEXT,evidence_count INTEGER DEFAULT 1,status TEXT DEFAULT 'candidate');
CREATE TABLE IF NOT EXISTS rule_stats(rule_key TEXT PRIMARY KEY,wins INTEGER DEFAULT 0,losses INTEGER DEFAULT 0,total_pnl REAL DEFAULT 0,updated TEXT);
CREATE TABLE IF NOT EXISTS quote_cache(ticker TEXT PRIMARY KEY,price REAL NOT NULL,source TEXT NOT NULL,quoted_at TEXT NOT NULL,updated_epoch REAL NOT NULL);
CREATE TABLE IF NOT EXISTS source_health(source_id TEXT PRIMARY KEY,name TEXT,last_attempt TEXT,last_success TEXT,last_status TEXT,failures INTEGER DEFAULT 0,backoff_until REAL DEFAULT 0,items_total INTEGER DEFAULT 0,last_error TEXT);
CREATE TABLE IF NOT EXISTS position_forecast_plans(
 id INTEGER PRIMARY KEY AUTOINCREMENT,position_id INTEGER NOT NULL,decision_id INTEGER NOT NULL,created TEXT NOT NULL,
 plan_version TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,event_type TEXT NOT NULL,forecast_decision TEXT NOT NULL,
 target_return REAL NOT NULL,stop_return REAL NOT NULL,target_price REAL NOT NULL,stop_price REAL NOT NULL,
 max_hold_days INTEGER NOT NULL,estimated_exit TEXT NOT NULL,target_probability REAL NOT NULL,confidence REAL NOT NULL,
 completeness REAL NOT NULL,partial_exit_pct REAL NOT NULL,runner_target_return REAL NOT NULL,runner_target_price REAL NOT NULL,
 runner_stop_return REAL NOT NULL,runner_stop_price REAL NOT NULL,bear_return REAL NOT NULL,base_return REAL NOT NULL,
 bull_return REAL NOT NULL,expected_return REAL NOT NULL,residual_return REAL NOT NULL,priced_in REAL NOT NULL,
 rationale TEXT NOT NULL,invalidation_json TEXT NOT NULL,forecast_hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_position_plan_active ON position_forecast_plans(position_id,active,created DESC);
CREATE TABLE IF NOT EXISTS position_lifecycle(
 position_id INTEGER PRIMARY KEY,phase TEXT NOT NULL DEFAULT 'OPEN',remaining_shares REAL NOT NULL,remaining_cost REAL NOT NULL,
 realized_pnl REAL NOT NULL DEFAULT 0,last_action_at TEXT,last_action_id TEXT,runner_plan_id INTEGER,updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS virtual_exit_actions(
 action_id TEXT PRIMARY KEY,position_id INTEGER NOT NULL,decision_id INTEGER NOT NULL,plan_id INTEGER NOT NULL,created TEXT NOT NULL,
 status TEXT NOT NULL,trigger TEXT NOT NULL,phase_before TEXT NOT NULL,phase_after TEXT,shares_requested REAL NOT NULL,
 shares_filled REAL,observed_price REAL,fill_price REAL,slippage_bps REAL,quote_source TEXT,quote_time TEXT,quote_age_seconds REAL,
 proceeds REAL,cost_released REAL,realized_pnl REAL,prior_cash REAL,new_cash REAL,reason TEXT NOT NULL,plan_json TEXT NOT NULL,
 evidence_json TEXT NOT NULL,committed_at TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS target_reforecasts(
 id INTEGER PRIMARY KEY AUTOINCREMENT,position_id INTEGER NOT NULL,prior_plan_id INTEGER NOT NULL,new_plan_id INTEGER,
 created TEXT NOT NULL,decision TEXT NOT NULL,observed_return REAL NOT NULL,elapsed_sessions INTEGER NOT NULL,
 velocity_ratio REAL NOT NULL,remaining_bull REAL NOT NULL,confidence REAL NOT NULL,reason TEXT NOT NULL,UNIQUE(position_id,prior_plan_id));
CREATE TABLE IF NOT EXISTS executor_runs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,created TEXT NOT NULL,status TEXT NOT NULL,positions_checked INTEGER NOT NULL,
 actions_committed INTEGER NOT NULL,holds_extended INTEGER NOT NULL,blocked_stale INTEGER NOT NULL,errors INTEGER NOT NULL,detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS position_event_links(position_id INTEGER NOT NULL,news_uid TEXT NOT NULL,linked_at TEXT NOT NULL,relevance REAL NOT NULL,direction REAL NOT NULL,scope TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(position_id,news_uid));
CREATE TABLE IF NOT EXISTS position_monitor_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,created TEXT NOT NULL,status TEXT NOT NULL,open_positions INTEGER NOT NULL,events_reviewed INTEGER NOT NULL,plans_changed INTEGER NOT NULL,detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS position_plan_changes(id INTEGER PRIMARY KEY AUTOINCREMENT,position_id INTEGER NOT NULL,created TEXT NOT NULL,prior_plan_id INTEGER,new_plan_id INTEGER,change_type TEXT NOT NULL,reason TEXT NOT NULL,event_uids_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event_impact_forecasts(
 decision_id INTEGER PRIMARY KEY,created TEXT NOT NULL,forecast_version TEXT NOT NULL,event_type TEXT NOT NULL,
 eligibility REAL NOT NULL,impact_score REAL NOT NULL,confidence REAL NOT NULL,data_completeness REAL NOT NULL,
 economic_impact REAL NOT NULL,priced_in REAL NOT NULL,residual_return REAL NOT NULL,bear_return REAL NOT NULL,
 base_return REAL NOT NULL,bull_return REAL NOT NULL,expected_return REAL NOT NULL,target_return REAL NOT NULL,
 stop_return REAL NOT NULL,max_hold_days INTEGER NOT NULL,target_probability REAL NOT NULL,allocation_pct REAL NOT NULL,
 partial_exit_pct REAL NOT NULL,runner_target REAL NOT NULL,runner_stop REAL NOT NULL,decision TEXT NOT NULL,
 invalidation_json TEXT NOT NULL,inputs_json TEXT NOT NULL,derivation_json TEXT NOT NULL,forecast_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS company_fundamentals(ticker TEXT PRIMARY KEY,cik TEXT,revenue REAL,operating_income REAL,cash REAL,debt REAL,shares REAL,market_cap REAL,backlog REAL,as_of TEXT,source_url TEXT);
"""

V5 = """
CREATE TABLE IF NOT EXISTS candidate_queue(id INTEGER PRIMARY KEY,created TEXT NOT NULL,event_id INTEGER,ticker TEXT NOT NULL,payload_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'NEW',decided TEXT);
CREATE INDEX IF NOT EXISTS ix_candq_status ON candidate_queue(status,id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_candq_event_ticker ON candidate_queue(event_id,ticker) WHERE event_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,news_uid TEXT UNIQUE,created TEXT NOT NULL,published TEXT,source TEXT,source_id TEXT,
 headline TEXT,event_type TEXT NOT NULL,scope TEXT NOT NULL,direction REAL NOT NULL,magnitude REAL NOT NULL,amount_usd REAL,
 tickers_json TEXT NOT NULL DEFAULT '[]',regions_json TEXT NOT NULL DEFAULT '[]',tokens TEXT,cluster_id INTEGER,
 novelty REAL NOT NULL DEFAULT 1,credibility REAL NOT NULL DEFAULT 0.5,age_hours REAL,stale INTEGER NOT NULL DEFAULT 0,llm_json TEXT);
CREATE INDEX IF NOT EXISTS ix_events_created ON events(created DESC);
CREATE INDEX IF NOT EXISTS ix_events_type ON events(event_type,created DESC);
CREATE TABLE IF NOT EXISTS evidence(
 id INTEGER PRIMARY KEY AUTOINCREMENT,event_id INTEGER NOT NULL,thesis_key TEXT,ticker TEXT,created TEXT NOT NULL,source TEXT,
 credibility REAL NOT NULL,strength REAL NOT NULL,novelty REAL NOT NULL,predictive_power REAL NOT NULL,direction REAL NOT NULL,
 cluster_id INTEGER,event_type TEXT NOT NULL,decay_days REAL NOT NULL,expiry_date TEXT NOT NULL,note TEXT,
 UNIQUE(event_id,thesis_key,ticker));
CREATE INDEX IF NOT EXISTS ix_evidence_thesis ON evidence(thesis_key,created DESC);
CREATE INDEX IF NOT EXISTS ix_evidence_ticker ON evidence(ticker,created DESC);
CREATE TABLE IF NOT EXISTS theses(
 id INTEGER PRIMARY KEY AUTOINCREMENT,key TEXT UNIQUE NOT NULL,name TEXT NOT NULL,parent_key TEXT,description TEXT,style TEXT,
 prior REAL NOT NULL,probability REAL NOT NULL,confidence_quality REAL NOT NULL DEFAULT 0,uncertainty REAL NOT NULL DEFAULT 0.25,
 trend REAL NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT 'Emerging',support_score REAL NOT NULL DEFAULT 0,
 contradiction_score REAL NOT NULL DEFAULT 0,net_strength REAL NOT NULL DEFAULT 0,evidence_mass REAL NOT NULL DEFAULT 0,
 last_evidence TEXT,last_updated TEXT,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS thesis_links(parent_key TEXT NOT NULL,child_key TEXT NOT NULL,weight REAL NOT NULL DEFAULT 0.5,PRIMARY KEY(parent_key,child_key));
CREATE TABLE IF NOT EXISTS thesis_companies(thesis_key TEXT NOT NULL,ticker TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'beneficiary',weight REAL NOT NULL DEFAULT 1,origin TEXT NOT NULL DEFAULT 'seed',PRIMARY KEY(thesis_key,ticker));
CREATE TABLE IF NOT EXISTS thesis_history(id INTEGER PRIMARY KEY AUTOINCREMENT,thesis_key TEXT NOT NULL,ts TEXT NOT NULL,prior REAL,probability REAL,uncertainty REAL,state TEXT,net_strength REAL,new_evidence INTEGER,detail_json TEXT);
CREATE INDEX IF NOT EXISTS ix_thesis_hist ON thesis_history(thesis_key,ts DESC);
CREATE TABLE IF NOT EXISTS companies(
 ticker TEXT PRIMARY KEY,name TEXT,sector TEXT,industry TEXT,country TEXT,currency TEXT,cik TEXT,market_cap REAL,adv_usd REAL,beta REAL,
 vol_daily REAL,dna TEXT,financial_strength REAL,management_quality REAL,competitive_moat REAL,innovation_quality REAL,
 institutional_support REAL,execution_quality REAL,completeness REAL NOT NULL DEFAULT 0,profile_json TEXT,updated TEXT);
CREATE TABLE IF NOT EXISTS forecasts(
 id INTEGER PRIMARY KEY AUTOINCREMENT,created TEXT NOT NULL,ticker TEXT NOT NULL,decision_id INTEGER,event_id INTEGER,thesis_key TEXT,
 direction TEXT NOT NULL,horizon_days INTEGER NOT NULL,p_up REAL,ev_gross REAL,ev_net REAL,risk_score REAL,confidence REAL,
 target_return REAL,stop_return REAL,regime TEXT,verdict TEXT NOT NULL,scenarios_json TEXT NOT NULL,inputs_json TEXT NOT NULL,hash TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_forecasts_ticker ON forecasts(ticker,created DESC);
CREATE TABLE IF NOT EXISTS predictions(
 id INTEGER PRIMARY KEY AUTOINCREMENT,created TEXT NOT NULL,kind TEXT NOT NULL,subject TEXT NOT NULL,claim TEXT NOT NULL,probability REAL NOT NULL,
 baseline_price REAL,benchmark_price REAL,expected_return REAL,horizon_date TEXT NOT NULL,source TEXT,event_type TEXT,thesis_key TEXT,
 forecast_id INTEGER,position_id INTEGER,resolved INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_pred_due ON predictions(resolved,horizon_date);
CREATE TABLE IF NOT EXISTS outcomes(
 prediction_id INTEGER PRIMARY KEY,observed_at TEXT NOT NULL,outcome REAL NOT NULL,realized_return REAL,benchmark_return REAL,
 abnormal_return REAL,brier REAL,detail_json TEXT);
CREATE TABLE IF NOT EXISTS narrative_states(
 id INTEGER PRIMARY KEY AUTOINCREMENT,thesis_key TEXT NOT NULL,ts TEXT NOT NULL,mentions REAL,strength REAL,velocity REAL,acceleration REAL,
 crowding REAL,exhaustion REAL,decay REAL,detail_json TEXT);
CREATE INDEX IF NOT EXISTS ix_narr ON narrative_states(thesis_key,ts DESC);
CREATE TABLE IF NOT EXISTS capital_flows(
 id INTEGER PRIMARY KEY AUTOINCREMENT,ticker TEXT NOT NULL,ts TEXT NOT NULL,kind TEXT NOT NULL,value REAL,shares REAL,
 detail_json TEXT,source TEXT,dedupe TEXT NOT NULL,UNIQUE(ticker,kind,dedupe));
CREATE INDEX IF NOT EXISTS ix_flows ON capital_flows(ticker,kind,ts DESC);
CREATE TABLE IF NOT EXISTS portfolio_snapshots(
 id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,equity REAL,cash REAL,gross REAL,drawdown REAL,regime TEXT,survival_probability REAL,
 max_stress_loss REAL,recovery_days REAL,exposures_json TEXT,stress_json TEXT);
CREATE TABLE IF NOT EXISTS regimes(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,regime TEXT NOT NULL,probs_json TEXT NOT NULL,features_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS research_tasks(
 id INTEGER PRIMARY KEY AUTOINCREMENT,created TEXT NOT NULL,kind TEXT NOT NULL,subject TEXT NOT NULL,description TEXT NOT NULL,priority REAL NOT NULL DEFAULT 0.5,
 status TEXT NOT NULL DEFAULT 'OPEN',auto INTEGER NOT NULL DEFAULT 0,attempts INTEGER NOT NULL DEFAULT 0,resolved_at TEXT,result TEXT,
 UNIQUE(kind,subject));
CREATE TABLE IF NOT EXISTS position_health(
 id INTEGER PRIMARY KEY AUTOINCREMENT,position_id INTEGER NOT NULL,ts TEXT NOT NULL,thesis_key TEXT,thesis_p REAL,thesis_trend REAL,health REAL,
 support REAL,contradiction REAL,signals_json TEXT);
CREATE INDEX IF NOT EXISTS ix_health ON position_health(position_id,ts DESC);
CREATE TABLE IF NOT EXISTS exit_signals(
 id INTEGER PRIMARY KEY AUTOINCREMENT,position_id INTEGER NOT NULL,created TEXT NOT NULL,trigger TEXT NOT NULL,severity REAL NOT NULL,reason TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'OPEN',consumed_at TEXT,UNIQUE(position_id,trigger,status));
CREATE TABLE IF NOT EXISTS market_series(symbol TEXT NOT NULL,day TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL NOT NULL,volume REAL,PRIMARY KEY(symbol,day));
CREATE TABLE IF NOT EXISTS series_state(symbol TEXT PRIMARY KEY,fetched_at TEXT NOT NULL,last_day TEXT);
CREATE TABLE IF NOT EXISTS model_params(key TEXT PRIMARY KEY,value_json TEXT NOT NULL,updated TEXT NOT NULL,reason TEXT);
CREATE TABLE IF NOT EXISTS source_stats(source TEXT NOT NULL,event_type TEXT NOT NULL,hits REAL NOT NULL DEFAULT 0,misses REAL NOT NULL DEFAULT 0,updated TEXT,PRIMARY KEY(source,event_type));
CREATE TABLE IF NOT EXISTS provider_calls(provider TEXT NOT NULL,ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_pcalls ON provider_calls(provider,ts);
CREATE TABLE IF NOT EXISTS provider_health(provider TEXT PRIMARY KEY,requests INTEGER DEFAULT 0,successes INTEGER DEFAULT 0,failures INTEGER DEFAULT 0,auth_failures INTEGER DEFAULT 0,rate_limits INTEGER DEFAULT 0,last_success TEXT,last_error TEXT,state TEXT NOT NULL DEFAULT 'HEALTHY',backoff_until REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS quote_consensus(id INTEGER PRIMARY KEY,ticker TEXT,created TEXT,session TEXT,status TEXT,consensus_price REAL,provider_count INTEGER,max_deviation REAL,execution_eligible INTEGER,providers_json TEXT,observations_json TEXT);
CREATE INDEX IF NOT EXISTS ix_quote_consensus ON quote_consensus(ticker,id DESC);
CREATE TABLE IF NOT EXISTS source_state(source_id TEXT PRIMARY KEY,etag TEXT,last_modified TEXT,updated TEXT);
CREATE TABLE IF NOT EXISTS task_schedule(task TEXT PRIMARY KEY,last_run TEXT,detail TEXT);
CREATE TABLE IF NOT EXISTS cycle_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,started TEXT NOT NULL,finished TEXT,status TEXT NOT NULL,mode TEXT,report_json TEXT);
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,subject TEXT,detail_json TEXT);
CREATE TABLE IF NOT EXISTS llm_usage(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,day TEXT NOT NULL,tier TEXT,task TEXT,provider TEXT,model TEXT,input_tokens INTEGER DEFAULT 0,output_tokens INTEGER DEFAULT 0,cost_usd REAL DEFAULT 0,ok INTEGER DEFAULT 0,cached INTEGER DEFAULT 0,error TEXT);
CREATE INDEX IF NOT EXISTS ix_llm_usage_day ON llm_usage(day,tier);
CREATE TABLE IF NOT EXISTS llm_cache(key TEXT PRIMARY KEY,task TEXT,response_json TEXT NOT NULL,created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intel_events(event_id INTEGER PRIMARY KEY,created TEXT NOT NULL,tier TEXT,model TEXT,analysis_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposed_narratives(key TEXT PRIMARY KEY,name TEXT,first_seen TEXT,last_seen TEXT,mentions INTEGER DEFAULT 1,status TEXT DEFAULT 'PROPOSED',sample_event_id INTEGER);
CREATE TABLE IF NOT EXISTS redteam_reviews(id INTEGER PRIMARY KEY AUTOINCREMENT,created TEXT NOT NULL,ticker TEXT NOT NULL,decision_id INTEGER,position_id INTEGER,net_conviction REAL,fatal_flaw INTEGER,valuation_view TEXT,gate TEXT,review_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_redteam_ticker ON redteam_reviews(ticker,created DESC);
CREATE TABLE IF NOT EXISTS tripwires(id INTEGER PRIMARY KEY AUTOINCREMENT,position_id INTEGER NOT NULL,created TEXT NOT NULL,kind TEXT NOT NULL,threshold REAL,event_type TEXT,description TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'OPEN',triggered_at TEXT);
CREATE TABLE IF NOT EXISTS forecast_revisions(id INTEGER PRIMARY KEY AUTOINCREMENT,ts TEXT NOT NULL,subject_type TEXT NOT NULL,subject TEXT NOT NULL,ticker TEXT,field TEXT NOT NULL,old_value REAL,new_value REAL,reason TEXT,conf_old REAL,conf_new REAL,detail_json TEXT);
CREATE INDEX IF NOT EXISTS ix_revisions ON forecast_revisions(ticker,ts DESC);
CREATE TABLE IF NOT EXISTS company_filings(accession TEXT PRIMARY KEY,ticker TEXT NOT NULL,form TEXT,filed TEXT,items TEXT,processed TEXT,ok INTEGER DEFAULT 0,extraction_json TEXT);
CREATE INDEX IF NOT EXISTS ix_filings_ticker ON company_filings(ticker,filed DESC);
CREATE TABLE IF NOT EXISTS company_knowledge(ticker TEXT PRIMARY KEY,summary_json TEXT NOT NULL,sources_json TEXT,updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS confidence_log(id INTEGER PRIMARY KEY AUTOINCREMENT,position_id INTEGER NOT NULL,ts TEXT NOT NULL,thesis REAL,forecast REAL,target REAL,position REAL,valuation TEXT,detail_json TEXT);
CREATE INDEX IF NOT EXISTS ix_conflog ON confidence_log(position_id,ts DESC);
"""

NEW_COLUMNS = {
    "events": {"gate": "TEXT"},
    "decisions": {"event_id": "INTEGER", "forecast_id": "INTEGER", "ev_net": "REAL", "thesis_key": "TEXT", "size_pct": "REAL", "origin": "TEXT"},
    "positions": {"thesis_key": "TEXT", "forecast_id": "INTEGER", "sector": "TEXT"},
}


def connect(path: str, readonly: bool = False) -> sqlite3.Connection:
    """Autocommit connection (explicit transactions via :func:`tx`). WAL + 60 s busy timeout."""
    if readonly:
        c = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=15, isolation_level=None, check_same_thread=False)
    else:
        c = sqlite3.connect(path, timeout=60, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=60000")
    if not readonly:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
    return c


@contextlib.contextmanager
def tx(c: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK. Re-entrant: an inner ``tx`` joins the outer transaction."""
    if c.in_transaction:
        yield c
        return
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
    except BaseException:
        c.execute("ROLLBACK")
        raise
    else:
        c.execute("COMMIT")


def tables(c) -> set:
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def columns(c, table: str) -> set:
    return {r[1] for r in c.execute("PRAGMA table_info(%s)" % table)}


_EXPECTED: Optional[Dict[str, list]] = None


def _statements(script: str) -> List[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


def expected_schema() -> Dict[str, list]:
    """The intended schema: {table: [(name, type, notnull, default, pk), ...]}, built by running the schema scripts in a scratch in-memory database."""
    global _EXPECTED
    if _EXPECTED is None:
        m = sqlite3.connect(":memory:")
        try:
            m.executescript(LEGACY)
            m.executescript(V5)
            _EXPECTED = {n: [(r[1], r[2] or "", r[3], r[4], r[5]) for r in m.execute('PRAGMA table_info("%s")' % n)]
                         for (n,) in m.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
        finally:
            m.close()
    return _EXPECTED


def schema_drift(c: sqlite3.Connection) -> Dict[str, List[str]]:
    """Tables/columns the code expects but this database does not have (read-only). {} = healthy.
    A database that began life as an OLDER install keeps its old tables: CREATE TABLE IF NOT EXISTS never adds columns to them."""
    have = tables(c)
    out: Dict[str, List[str]] = {}
    for t, cols in expected_schema().items():
        if t not in have:
            out[t] = ["<table missing>"]
            continue
        present = {r[1] for r in c.execute('PRAGMA table_info("%s")' % t)}
        miss = [col[0] for col in cols if col[0] not in present]
        if miss:
            out[t] = miss
    return out


def reconcile_columns(c: sqlite3.Connection) -> Tuple[List[str], List[str]]:
    """Add every missing column to every existing table (data preserved). Returns (added 'table.column' list, unfixable list)."""
    added: List[str] = []
    unfixable: List[str] = []
    exp = expected_schema()
    for t, missing in schema_drift(c).items():
        if missing == ["<table missing>"]:
            continue
        defs = {col[0]: col for col in exp[t]}
        for name in missing:
            _, typ, notnull, dflt, pk = defs[name]
            if pk:
                unfixable.append("%s.%s (primary key)" % (t, name))
                continue
            ddl = 'ALTER TABLE "%s" ADD COLUMN "%s" %s' % (t, name, typ)
            if dflt is not None:
                ddl += " DEFAULT %s" % dflt
            elif notnull:
                ddl += " DEFAULT %s" % ("''" if any(k in typ.upper() for k in ("CHAR", "TEXT", "CLOB")) or not typ else "0")
            try:
                c.execute(ddl)
                added.append("%s.%s" % (t, name))
            except sqlite3.OperationalError as e:
                unfixable.append("%s.%s (%s)" % (t, name, e))
    return added, unfixable


def create_schema(c: sqlite3.Connection) -> Tuple[List[str], List[str]]:
    """Create missing tables, REPAIR existing ones, then create indexes (an index on a column an old table lacked used to be impossible)."""
    stmts = _statements(LEGACY) + _statements(V5)
    for s in stmts:
        if s.upper().startswith("CREATE TABLE"):
            c.execute(s)
    added, unfixable = reconcile_columns(c)
    for s in stmts:
        if not s.upper().startswith("CREATE TABLE"):
            try:
                c.execute(s)
            except sqlite3.OperationalError as e:
                unfixable.append("%s (%s)" % (" ".join(s.split())[:60], e))
    return added, unfixable


def package_build() -> str:
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "BUILD")) as f:
            return f.read().strip() or "?"
    except OSError:
        return "?"


def error_baseline(c: sqlite3.Connection, now=None) -> Optional[str]:
    """Errors from cycles that started BEFORE this ISO time are history (an earlier build, or acknowledged by the operator) and do not raise flags.
    Nothing is deleted: the rows stay as an audit trail. A baseline in the future relative to ``now`` is ignored (a build cannot have been installed tomorrow)."""
    vals = [r[0] for r in c.execute("SELECT value FROM meta WHERE key IN ('build_since','errors_ack_at')") if r[0]]
    if now is not None:
        vals = [v for v in vals if v <= iso(now)]                    # discard future markers FIRST, then take the latest of what remains
    return max(vals) if vals else None


def migrate(c: sqlite3.Connection, starting_cash: float = 1_000_000.0) -> dict:
    """Idempotent. Safe to run on an empty DB, a v1-v4 production DB, or a v5 DB."""
    report = {"renamed": [], "added_columns": [], "created_ledger": False}
    have = tables(c)
    if "theses" in have and "decision_id" in columns(c, "theses") and "decision_theses" not in have:
        c.execute("ALTER TABLE theses RENAME TO decision_theses")
        report["renamed"].append("theses->decision_theses")
    repaired, unfixable = create_schema(c)
    report["repaired_columns"], report["unfixable"] = repaired, unfixable
    for table, cols in NEW_COLUMNS.items():
        have_cols = columns(c, table)
        for name, ddl in cols.items():
            if name not in have_cols:
                c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))
                report["added_columns"].append("%s.%s" % (table, name))
    with tx(c):
        if not c.execute("SELECT 1 FROM meta WHERE key='initialized'").fetchone():
            now = iso()
            c.execute("INSERT INTO meta VALUES('initialized',?)", (now,))
            c.execute("INSERT OR IGNORE INTO meta VALUES('rules_version','5.0.0')")
        if not c.execute("SELECT 1 FROM ledger WHERE kind='START'").fetchone():
            c.execute("INSERT INTO ledger(created,kind,amount,ref,balance) VALUES(?,?,?,?,?)",
                      (iso(), "START", starting_cash, "initial virtual capital", starting_cash))
            report["created_ledger"] = True
        c.execute("INSERT OR REPLACE INTO meta VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        b = package_build()
        cur = c.execute("SELECT value FROM meta WHERE key='build'").fetchone()
        if not cur or cur[0] != b:                                   # a new build is running: errors from earlier builds become history
            c.execute("INSERT OR REPLACE INTO meta VALUES('build',?)", (b,))
            c.execute("INSERT OR REPLACE INTO meta VALUES('build_since',?)", (iso(),))
        c.execute("UPDATE meta SET value='5.0.0' WHERE key='rules_version'")
        # Seed lifecycle rows for legacy open positions that predate the lifecycle table.
        for r in c.execute("SELECT id,shares,cost FROM positions WHERE status='OPEN'").fetchall():
            c.execute("INSERT OR IGNORE INTO position_lifecycle(position_id,phase,remaining_shares,remaining_cost,updated) VALUES(?,?,?,?,?)",
                      (r["id"], "OPEN", r["shares"], r["cost"], iso()))
    return report


def open_db(path: str, starting_cash: float = 1_000_000.0) -> sqlite3.Connection:
    import os
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    c = connect(path)
    migrate(c, starting_cash)
    return c


# ---- small helpers used everywhere ----------------------------------------------------------
def cash(c) -> float:
    r = c.execute("SELECT balance FROM ledger ORDER BY id DESC LIMIT 1").fetchone()
    return float(r[0]) if r else 0.0


def start_capital(c) -> float:
    r = c.execute("SELECT amount FROM ledger WHERE kind='START' ORDER BY id LIMIT 1").fetchone()
    return float(r[0]) if r else 0.0


def param(c, key: str, default=None):
    r = c.execute("SELECT value_json FROM model_params WHERE key=?", (key,)).fetchone()
    if not r:
        return default
    try:
        return json.loads(r[0])
    except ValueError:
        return default


def set_param(c, key: str, value, reason: str = "") -> None:
    c.execute("INSERT INTO model_params(key,value_json,updated,reason) VALUES(?,?,?,?) "
              "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated=excluded.updated,reason=excluded.reason",
              (key, json.dumps(value, sort_keys=True), iso(), reason))


def audit(c, actor: str, action: str, subject: str = "", detail: Optional[Dict] = None) -> None:
    c.execute("INSERT INTO audit_log(ts,actor,action,subject,detail_json) VALUES(?,?,?,?,?)",
              (iso(), actor, action, subject, json.dumps(detail or {}, sort_keys=True, default=str)))


def due(c, task: str, every_hours: float, now=None) -> bool:
    """True if ``task`` has not run within ``every_hours``. Does not record a run."""
    from .timeutil import parse_ts
    r = c.execute("SELECT last_run FROM task_schedule WHERE task=?", (task,)).fetchone()
    last = parse_ts(r[0]) if r else None
    return last is None or ((now or utcnow()) - last).total_seconds() >= every_hours * 3600


def mark_run(c, task: str, detail: str = "", now=None) -> None:
    """Record that ``task`` ran. Uses the SAME clock as due(): pass the cycle's ``now`` (a mixed real/injected clock made rate limits wrong)."""
    c.execute("INSERT INTO task_schedule(task,last_run,detail) VALUES(?,?,?) ON CONFLICT(task) DO UPDATE SET last_run=excluded.last_run,detail=excluded.detail",
              (task, iso(now), detail))
