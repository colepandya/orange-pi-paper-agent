# Free options data (build 40)

Nothing here costs money and no new key is needed.

| Feed | Key | Gives | Notes |
|---|---|---|---|
| CBOE delayed quotes (cdn.cboe.com) | none | the full chain: bid, ask, IV, delta, volume, open interest | primary; unofficial public JSON |
| Alpaca indicative (data.alpaca.markets) | your existing free Alpaca keys | bid, ask, IV, delta | free "indicative" feed |
| Nasdaq (api.nasdaq.com) | none | bid, ask, last, volume, open interest | no IV: it is solved from the price |
| Yahoo (query1.finance.yahoo.com) | none | bid, ask, IV, volume, open interest | unofficial; the most likely to break |

All are DELAYED about 15 minutes. So a chain is never treated as "the price now": it tells the bot which contracts exist and are liquid, the market's implied volatility and the real spread, and the option is then repriced at the LIVE stock price with that volatility ("anchored"). The four feeds are fetched side by side (a cache holds a chain for 4 minutes, a total failure for 90 s), the merged quote is the MEDIAN, and a quote the feeds disagree about by more than 15% is thrown away.

Check them from the Pi at any time: `cd /opt/paper-agent && sudo -u paperagent env PYTHONPATH=/opt/paper-agent python3 -m paper_agent optionsprobe NVDA`
It prints each feed's status, speed and contract count, then the call and put the bot would pick and the price it would pay.

If none answers the bot says so: the option is priced by the model and labelled MODEL (Settings -> Instruments can forbid that: then the stock is traded instead). The Positions page shows each feed's health and how far the model is from the real market.
These feeds are public, undocumented surfaces that can change without notice; that is why there are four and why failure is a normal, handled case.
