#!/usr/bin/env python3
import json, os, sys, time
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','app'))
from agent import fetch_feed,fetch_gdelt
cfg=json.load(open(sys.argv[1] if len(sys.argv)>1 else os.path.join(os.path.dirname(__file__),'..','config','config.json')))
failed=0
for f in cfg.get('feeds',[]):
 try:
  t=time.time(); n=len(fetch_feed(f)); print(f"PASS RSS {f['name']}: items={n} seconds={time.time()-t:.1f}")
  if n<1: failed+=1
 except Exception as e: failed+=1; print(f"FAIL RSS {f['name']}: {type(e).__name__} code={getattr(e,'code','')} detail={str(e)[:100]}")
g=cfg.get('gdelt',{})
if g.get('enabled'):
 q=g.get('queries',['NVIDIA'])[0]
 try:
  t=time.time(); n=len(fetch_gdelt(q,min(10,g.get('max_records_per_query',10)))); print(f"PASS GDELT: items={n} seconds={time.time()-t:.1f}")
  if n<1: failed+=1
 except Exception as e: failed+=1; print(f"FAIL GDELT: {type(e).__name__} code={getattr(e,'code','')} detail={str(e)[:100]}")
sys.exit(1 if failed else 0)
