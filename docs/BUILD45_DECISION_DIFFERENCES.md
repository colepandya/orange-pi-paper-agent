# Build 45: every real decision that differs from Build 44

Computed by replaying all 15,143 stored events through the complete candidate-creation path of each build (classifier, matcher, worthiness gate, candidate gate) and comparing with the real decision table. Hypothetical: it shows which decisions would not have been CREATED; it does not replay prices.

```
candidates (event,ticker) produced from 15,143 stored events:  Build 44 = 371   Build 45 = 331   removed = 71   added = 31

REAL DECISIONS THAT BUILD 45 WOULD NOT HAVE CREATED: 34 decision rows (34 event/ticker pairs)
   by status: {'REJECTED': 33, 'VETOED': 1}
   EXECUTED (money was traded) among them: 0
   why they disappear: {'gate: not_an_event': 30, 'gate: no_company_matched': 2, 'entity: ticker no longer matched': 2}
```

## Real decisions Build 45 would not have created

| Decision | Ticker | Status | Why | Headline |
|---|---|---|---|---|
| #4985 | EEFT | REJECTED | not_an_event: question | What Euronet Worldwide (EEFT) Said on Its Q3 Earnings Call |
| #5031 | GIL | REJECTED | not_an_event: solicitation | ATTENTION NYSE: GIL INVESTORS: Contact Berger Montague About a Gildan Activewear Inc. Clas |
| #5049 | MSFT | REJECTED | not_an_event: no_concrete_event | Microsoft reveals strong cloud growth amid financial reporting overhaul |
| #5077 | GNRC | REJECTED | not_an_event: question | What Generac (GNRC) Said on Its Q3 Earnings Call |
| #5090 | GOOGL | VETOED | not_an_event: no_concrete_event | Google expands AI Search ads globally as core ad growth slows |
| #5132 | MSFT | REJECTED | ticker no longer matched | Google's post-earnings stock pullback could be a 'buying opportunity': Analyst |
| #5123 | MSFT | REJECTED | not_an_event: commentary | Key Takeaways From Microsoft and ServiceNow Earnings |
| #5124 | NOW | REJECTED | not_an_event: commentary | Key Takeaways From Microsoft and ServiceNow Earnings |
| #5136 | AAPL | REJECTED | not_an_event: no_concrete_event | Global Product Take-Back Program Market Report 2026 / Capitalize on 10.7% CAGR to $63.73 B |
| #5137 | AMZN | REJECTED | not_an_event: no_concrete_event | Global Product Take-Back Program Market Report 2026 / Capitalize on 10.7% CAGR to $63.73 B |
| #5138 | SONY | REJECTED | not_an_event: no_concrete_event | Global Product Take-Back Program Market Report 2026 / Capitalize on 10.7% CAGR to $63.73 B |
| #5128 | MU | REJECTED | not_an_event: no_concrete_event | AI Spending Is Nearing $1 Trillion Annually. These 2 Stocks Are Most at Risk If It Slows. |
| #5129 | NVDA | REJECTED | not_an_event: no_concrete_event | AI Spending Is Nearing $1 Trillion Annually. These 2 Stocks Are Most at Risk If It Slows. |
| #5168 | GOOGL | REJECTED | not_an_event: price_recap | Google shares slump amid fears of AI overspending |
| #5181 | EQIX | REJECTED | not_an_event: question | What Equinix (EQIX) Said on Its Q3 Earnings Call |
| #5188 | GOOGL | REJECTED | not_an_event: commentary | Alphabet’s Google Cloud Biz Did Its Job. Why the Stock Is Falling. |
| #5192 | MSFT | REJECTED | not_an_event: question | What Microsoft (MSFT) Said on Its Q1 Earnings Call |
| #5223 | QUBT | REJECTED | not_an_event: stock_list | 5 Quantum Computing Stocks Investors Should Be Watching in 2027 |
| #5220 | NWAX | REJECTED | no_company_matched | Mark Cuban Warns Hyperscaler Borrowing Could Create A “Debt Bubble In The United States Of |
| #5239 | NWAX | REJECTED | ticker no longer matched | Warren urges DOJ criminal probe of Bank of America over anti-money-laundering violations |
| #5251 | MU | REJECTED | not_an_event: no_concrete_event | Memory Mania Has Micron Back Near $1,000 |
| #5258 | MXF | REJECTED | not_an_event: no_concrete_event | THE MEXICO FUND, INC. PAYS DISTRIBUTION |
| #5266 | HON | REJECTED | not_an_event: solicitation | INVESTOR ALERT: Pomerantz Law Firm Reminds Investors with Losses on their Investment in Ho |
| #5265 | HONA | REJECTED | not_an_event: solicitation | INVESTOR ALERT: Pomerantz Law Firm Reminds Investors with Losses on their Investment in Ho |
| #5282 | LLY | REJECTED | not_an_event: commentary | We're raising our price target on Eli Lilly as the obesity-drug boom rolls on |
| #5310 | GOOGL | REJECTED | not_an_event: no_concrete_event | Wall Street's AI fears slash Google value by $500bn |
| #5314 | AMZN | REJECTED | not_an_event: commentary | We're cutting our Amazon price target after a tough quarter. But it isn't all bad |
| #5330 | TSLA | REJECTED | not_an_event: no_concrete_event | Musk's Massive Marketing Miss Cost $10 Trillion |
| #5344 | RIVN | REJECTED | not_an_event: question | What Rivian Automotive (RIVN) Said on Its Q3 Earnings Call |
| #5362 | TENX | REJECTED | not_an_event: solicitation | ATTENTION NASDAQ: TENX INVESTORS: Contact Berger Montague About a Tenax Therapeutics, Inc. |
| #5363 | OTAI | REJECTED | no_company_matched | Ukraine invests in firepower as it inflicts a new Russian casualty record |
| #5373 | CVX | REJECTED | not_an_event: price_recap | Chevron and Exxon hit all-time highs as refining profits soar in reaction to Iran and Ukra |
| #5372 | XOM | REJECTED | not_an_event: price_recap | Chevron and Exxon hit all-time highs as refining profits soar in reaction to Iran and Ukra |
| #5375 | TIGR | REJECTED | not_an_event: solicitation | SHAREHOLDER ALERT: Bronstein Gewirtz & Grossman, LLC Announces Class Action Against UP Fin |

**None was executed.** Judgement calls a reader may disagree with: #5049 (MSFT 'reveals strong cloud growth'), #5090 (GOOGL 'expands AI Search ads', vetoed anyway). Entity corrections (false matches removed): the NWAX, OTAI and MSFT-in-a-Google-story rows.

## Why these are right

A question, an earnings-call recap, a law-firm solicitation, a market-research page, a stock-pick list, a publisher-voice price target and a price recap each report nothing that has happened at the company. Identifying the company correctly does not make them tradable events.
