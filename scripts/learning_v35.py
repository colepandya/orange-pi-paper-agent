#!/usr/bin/env python3
import argparse,json,sys
sys.path.insert(0,'/opt/paper-agent/app');import learning_v35 as l
p=argparse.ArgumentParser();p.add_argument('job',choices=['init','capture','readiness']);a=p.parse_args();d=l.connect();l.init(d);out={'version':l.VERSION}
if a.job=='init':out={'version':l.register_strategy(d)}
elif a.job=='capture':out={'captured':l.capture_missing(d)}
elif a.job=='readiness':out=l.readiness(d)
d.close();print(json.dumps(out,indent=2,sort_keys=True))
