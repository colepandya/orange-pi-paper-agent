#!/usr/bin/env python3
import argparse,json,os,sys
sys.path.insert(0,'/opt/paper-agent/app');import v34_engine as e
p=argparse.ArgumentParser();p.add_argument('job',choices=['init','reconcile','research']);p.add_argument('--db',default=e.DB);a=p.parse_args()
with e.writer_lock():
 d=e.connect(a.db);e.init(d);result={'version':e.VERSION}
 if a.job=='reconcile':result=e.reconcile(d)
 elif a.job=='research':result=e.challenger_run(d)
 d.close()
print(json.dumps(result,indent=2,sort_keys=True))
