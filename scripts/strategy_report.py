#!/usr/bin/env python3
import importlib.util,json,sqlite3
p='/opt/paper-agent/app/strategy_v3.py';s=importlib.util.spec_from_file_location('v3',p);v3=importlib.util.module_from_spec(s);s.loader.exec_module(v3);d=sqlite3.connect('/var/lib/paper-agent/paper-agent.db');d.row_factory=sqlite3.Row;v3.init(d);print(json.dumps(v3.performance_report(d),indent=2));d.close()
