# What is not verified, and what is not possible

**Verified:** 74 offline tests, 20-day randomised soak with ledger reconciliation, Python-3.9 syntax check, migration of a legacy-shaped DB.

**Not verified (no network in the build environment) — check on the Pi:**
* News feed URLs: run `python3 -m paper_agent probe`; dead feeds back off automatically and appear in the *System* tab.
* Quote providers' JSON shapes (Massive/Tiingo/EODHD especially): run `python3 -m paper_agent quote MSFT` during market hours. Execution needs ≥2 providers agreeing with timestamps ≤180 s, so a mis-parsed provider fails *closed* (no trade), never open.
* Yahoo's unofficial chart endpoint (regime, volatility, outcome scoring) and the SEC / Finnhub fundamentals parsers (fixture-tested only).
* The optional LLM analyst (schema validation tested; live API call not).
* `systemd` units were syntax-reviewed, not run through `systemd-analyze`.

**Not possible with free data:** 13F / institutional ownership changes, true ETF fund flows (a dollar-volume *proxy* is used and labelled), analyst estimates beyond Finnhub's free tier. These raise research tasks instead of being faked.

**Design limits:** long-only (bearish events → AVOID); US equities only; daily-bar volatility; theses are seeded by hand (`theses_seed.py`) and re-weighted by evidence, not invented. Learning needs time: expect 4-8 weeks of closed forecasts before calibration moves anything (minimum 20 samples per event type, 40 overall).
**This is a research/paper system. Nothing here is investment advice, and paper results do not guarantee live results.**
