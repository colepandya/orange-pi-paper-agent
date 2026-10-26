#!/usr/bin/env python3
import py_compile,shutil,sys
from pathlib import Path
p=Path('/opt/paper-agent/app/agent.py');b=Path('/opt/paper-agent/app/agent.py.before-v22');m=Path('/opt/paper-agent/app/experiment_execution.py')
if not p.exists():raise SystemExit('FAIL agent.py missing')
s=p.read_text()
if 'EXPERIMENT_V22_DELEGATE' in s:print('PASS already installed');sys.exit(0)
a=s.find('def execute_virtual_buy(');z=s.find('\ndef trading_days_since(',a)
if a<0 or z<0:raise SystemExit('FAIL expected execution function not found')
shutil.copy2(p,b)
new="""EXPERIMENT_V22_DELEGATE=True
def execute_virtual_buy(store,cfg,watchlist,ticker,score,decision_id,price_provider=None):
    from experiment_execution import execute_virtual_buy as enhanced_buy
    return enhanced_buy(store,cfg,watchlist,ticker,score,decision_id)
"""
try:p.write_text(s[:a]+new+s[z:]);py_compile.compile(str(p),doraise=True);py_compile.compile(str(m),doraise=True);print('PASS enhanced execution installed')
except Exception:shutil.copy2(b,p);raise
