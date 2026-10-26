import importlib.util,os,sqlite3,tempfile,unittest
from unittest.mock import patch
ROOT=os.path.abspath(os.path.join(os.path.dirname(__file__),".."));sp=importlib.util.spec_from_file_location("e10",os.path.join(ROOT,"app","experiment_execution.py"));e=importlib.util.module_from_spec(sp);sp.loader.exec_module(e)
class Store:
 def __init__(self,p):
  self.db=sqlite3.connect(p);self.db.row_factory=sqlite3.Row;self.db.executescript("create table decisions(id integer primary key,status,reason,amount,entry_price,shares,score,action,ticker);create table ledger(id integer primary key,created,kind,amount,ref,balance);create table positions(id integer primary key,ticker,theme,opened,entry_price,shares,cost,decision_id,status);insert into ledger values(1,'x','START',1000000,'x',1000000);")
 def cash(self):return self.db.execute("select balance from ledger order by id desc limit 1").fetchone()[0]
CFG={"portfolio":{"max_single_stock_pct":.5,"max_theme_pct":.8,"max_deployed_pct":.9,"slippage_bps":0}};W={"X":{"theme":"Tech","exchange":"NASDAQ"}}
class MinimumTen(unittest.TestCase):
 def test_exact_ladder(self):self.assertEqual([e.allocation_pct(x) for x in (64,65,79,80,89,90,94,95,100)],[0,.1,.1,.2,.2,.35,.35,.5,.5])
 def test_score_65_executes_ten_percent(self):
  with tempfile.TemporaryDirectory() as t:
   st=Store(os.path.join(t,"x"));st.db.execute("insert into decisions values(1,'PROPOSED','r',null,null,null,65,'BUY','X')");st.db.commit()
   with patch.object(e,"resolve_quote",return_value={"price":100,"source":"TEST","quoted_at":"t"}):e.execute_virtual_buy(st,CFG,W,"X",65,1)
   row=st.db.execute("select status,amount from decisions where id=1").fetchone();self.assertEqual(row[0],"EXECUTED");self.assertEqual(row[1],100000)
 def test_capacity_below_ten_percent_defers(self):
  with tempfile.TemporaryDirectory() as t:
   st=Store(os.path.join(t,"x"));st.db.execute("update ledger set balance=150000 where id=1");st.db.execute("insert into positions values(1,'Y','Tech','x',1,850000,850000,99,'OPEN')");st.db.execute("insert into decisions values(1,'PROPOSED','r',null,null,null,65,'BUY','X')");st.db.commit()
   with patch.object(e,"resolve_quote",return_value={"price":100,"source":"TEST","quoted_at":"t"}):e.execute_virtual_buy(st,CFG,W,"X",65,1)
   self.assertEqual(st.db.execute("select status from decisions where id=1").fetchone()[0],"CAPACITY_DEFERRED")
if __name__=="__main__":unittest.main()
