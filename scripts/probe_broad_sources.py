#!/usr/bin/env python3
import importlib.util,json,os,sys,time
ROOT=os.path.abspath(os.path.join(os.path.dirname(__file__),'..'))
spec=importlib.util.spec_from_file_location('broad',os.path.join(ROOT,'app','broad_collector.py')); broad=importlib.util.module_from_spec(spec); spec.loader.exec_module(broad)
cfg=json.load(open(sys.argv[1] if len(sys.argv)>1 else os.path.join(ROOT,'config','sources.json')))
mandatory={'sec_latest','federal_register_pi','usaspending_awards'}; passed=set(); failed=[]
for src in cfg['sources']:
 try:
  started=time.time(); items,status=broad.fetch_source(src); valid=sum(1 for x in items if x.get('title'))
  if status!=200 or valid<1: raise RuntimeError(f'status={status} valid_items={valid}')
  passed.add(src['id']); print(f"PASS {src['id']} status={status} items={valid} seconds={time.time()-started:.2f}")
 except Exception as e:
  failed.append(src['id']); print(f"FAIL {src['id']} {type(e).__name__} code={getattr(e,'code','')} detail={str(e)[:180]}")
missing=sorted(mandatory-passed)
print(json.dumps({'passed':len(passed),'failed':failed,'mandatory_missing':missing}))
if missing or len(passed)<5: sys.exit(1)
