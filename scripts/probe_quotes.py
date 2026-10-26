#!/usr/bin/env python3
import importlib.util,os,sqlite3,tempfile
p=os.path.join(os.path.dirname(__file__),'..','app','experiment_execution.py');s=importlib.util.spec_from_file_location('e',p);e=importlib.util.module_from_spec(s);s.loader.exec_module(e)
with tempfile.TemporaryDirectory() as t:
 d=sqlite3.connect(os.path.join(t,'q'));d.row_factory=sqlite3.Row
 for ticker,exchange in [('AAPL','NASDAQ'),('IBM','NYSE'),('AMTM','NYSE')]:
  q=e.resolve_quote(d,ticker,{'exchange':exchange});print(ticker,'price=',q['price'],'source=',q['source'],'errors=',q['errors'])
  if ticker in ('AAPL','IBM') and not q['price']:raise SystemExit('FAIL: mandatory quote unavailable for '+ticker)
print('PASS: mandatory quote fallback chain returns prices')
