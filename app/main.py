#!/usr/bin/env python3
import argparse,json,os,sys,threading,time
sys.path.insert(0,os.path.dirname(__file__))
from agent import Store,ingest_and_decide,review_positions,snapshot
from web import serve

def load(p):
    with open(p) as f:return json.load(f)
def run_cycle(store,cfg,watch):
    stats=ingest_and_decide(store,cfg,watch); closed=review_positions(store,cfg); equity=snapshot(store,watch); print(json.dumps({'stats':stats,'closed':closed,'equity':equity}),flush=True)
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--config',required=True); ap.add_argument('--watchlist'); ap.add_argument('--db',required=True); ap.add_argument('--once',action='store_true'); a=ap.parse_args(); cfg=load(a.config); watch=load(a.watchlist or os.path.join(os.path.dirname(a.config),'watchlist.json')); store=Store(a.db,cfg['portfolio']['starting_cash'])
    if a.once: run_cycle(store,cfg,watch); return
    th=threading.Thread(target=serve,args=(store,watch,cfg['runtime']['bind_host'],cfg['runtime']['bind_port']),daemon=True); th.start()
    while True:
        try: run_cycle(store,cfg,watch)
        except Exception as e: print(json.dumps({'cycle_error':type(e).__name__,'detail':str(e)[:200]}),flush=True)
        time.sleep(cfg['runtime']['loop_seconds'])
if __name__=='__main__':main()
