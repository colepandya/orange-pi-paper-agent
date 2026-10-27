#!/usr/bin/env python3
import json,time
try:
 from . import market_clock_v401 as clock,quote_refresh_v401 as q
except ImportError:
 import market_clock_v401 as clock,quote_refresh_v401 as q
while True:
 d=q.connect()
 try:a=q.refresh(d,q.tickers(d));print(json.dumps({"quotes":len(a),"session":clock.state()["session"]}),flush=True);wait=clock.state()["refresh_seconds"]
 finally:d.close()
 time.sleep(wait)
