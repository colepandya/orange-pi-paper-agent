import importlib.util,os,sqlite3,tempfile,unittest
from unittest.mock import patch
ROOT=os.path.abspath(os.path.join(os.path.dirname(__file__),'..'));s=importlib.util.spec_from_file_location('e',os.path.join(ROOT,'app','experiment_execution.py'));e=importlib.util.module_from_spec(s);s.loader.exec_module(e)
class Store:
 def __init__(self,p):self.db=sqlite3.connect(p);self.db.row_factory=sqlite3.Row;self.db.executescript("create table decisions(id integer primary key,status,reason,amount,entry_price,shares,score,action,ticker);create table ledger(id integer primary key,created,kind,amount,ref,balance);create table positions(id integer primary key,ticker,theme,opened,entry_price,shares,cost,decision_id,status);insert into ledger values(1,'x','START',1000000,'x',1000000);")
 def cash(self):return self.db.execute('select balance from ledger order by id desc limit 1').fetchone()[0]
CFG={'portfolio':{'max_single_stock_pct':.5,'max_theme_pct':.8,'max_deployed_pct':.9,'slippage_bps':0}};W={'X':{'theme':'Tech','exchange':'NASDAQ'}}
class T(unittest.TestCase):
 def test_ladder(self):self.assertEqual([e.allocation_pct(x) for x in (64,65,79,80,89,90,94,95)],[0,.1,.1,.2,.2,.35,.35,.5])
 def test_buy_10(self):
  with tempfile.TemporaryDirectory() as t:
   st=Store(os.path.join(t,'x'));st.db.execute("insert into decisions values(1,'PROPOSED','r',null,null,null,65,'BUY','X')");st.db.commit()
   with patch.object(e,'resolve_quote',return_value={'price':100,'source':'T','quoted_at':'t'}):e.execute_virtual_buy(st,CFG,W,'X',65,1)
   r=st.db.execute('select status,amount from decisions').fetchone();self.assertEqual((r[0],r[1]),('EXECUTED',100000))
 def test_await(self):
  with tempfile.TemporaryDirectory() as t:
   st=Store(os.path.join(t,'x'));st.db.execute("insert into decisions values(1,'PROPOSED','r',null,null,null,65,'BUY','X')");st.db.commit()
   with patch.object(e,'resolve_quote',return_value={'price':None,'errors':['x']}):e.execute_virtual_buy(st,CFG,W,'X',65,1)
   self.assertEqual(st.db.execute('select status from decisions').fetchone()[0],'AWAITING_PRICE')
 def test_google(self):
  with patch.object(e,'get',return_value=(b'<div data-last-price="123.45"></div>',200)):self.assertEqual(e.google_finance_quote('X','NASDAQ')[0],123.45)
if __name__=='__main__':unittest.main()

class QuoteFallbackTests(unittest.TestCase):
 def test_yahoo_regular_market_price(self):
  payload=b'{"chart":{"result":[{"meta":{"regularMarketPrice":321.5,"regularMarketTime":1790000000},"indicators":{"quote":[{"close":[320.0,321.5]}]}}],"error":null}}'
  with patch.object(e,'get',return_value=(payload,200)):
   q=e.yahoo_chart_quote('AAPL');self.assertEqual(q[0],321.5);self.assertEqual(q[1],'YAHOO_CHART')
 def test_yahoo_close_fallback(self):
  payload=b'{"chart":{"result":[{"meta":{},"indicators":{"quote":[{"close":[null,100.0,101.25]}]}}],"error":null}}'
  with patch.object(e,'get',return_value=(payload,200)):
   self.assertEqual(e.yahoo_chart_quote('X')[0],101.25)
 def test_stooq_page_parser(self):
  with patch.object(e,'get',return_value=(b'<html>Last $ 347.7250 Date 2026</html>',200)):
   self.assertEqual(e.stooq_quote('AAPL')[0],347.725)
