# Waiting for the network side by side

A cycle used to spend most of its time (collecting feeds: 46 s of a 59 s median cycle) waiting for servers one at a time. From build 38 the waiting is shared. The rule that keeps it safe:
**threads only wait for the network; everything that reads or writes the database, or decides, stays in one place, in a fixed order.** Results come back in the order the jobs were given, whichever
server answered first, so a decision cannot depend on who was fastest. `paper_agent/parallel.py` is the one helper; at most 2 requests to a server at once (3 per quote provider), GDELT's one-per-5.5-seconds
rule is kept, a hard overall deadline cuts off a server that hangs, and one failure never affects another.

| Where | What is shared | What stays in order |
|---|---|---|
| News feeds (`sources.collect`) | the downloads | parsing, ETag, health, storing, in source order |
| Article pages (`reader.enrich`) | robots.txt + download | analysis and storing, in rank order |
| Price refresh (`prices.run`) | the chart downloads (about 6 a second) | storing, in symbol order |
| Quotes for exits and entries (`quotes.prefetch`) | every provider for every stock, ahead of the stage | the consensus logic itself is unchanged; free-tier budgets are spent in priority order while planning |

Setting: Settings -> Refresh times -> "Network requests in flight at once" (default 6; 1 = the old one-after-another behaviour). The Sources table shows how long each feed took.
Not shared, by design: decisions, exits, anything that writes. Candidates for later: company-profile refresh, Finnhub company news, missed-trade price fetch.
