# Running a fair trial: one fixed strategy

A 2-4 week trial only measures something if the strategy stays the same while it runs. Two things could quietly change it, and both are now controlled.

## 1. The system no longer rewrites itself (Settings -> Refresh times -> "What the system does with what it learns")
* **observe (default)**: it keeps learning from resolved predictions, but only RECORDS what it would change. Probability calibration, event-type power, move sizes and thesis priors are stored as
  `proposed:*`; per-source hit/miss counts are stored apart as `observed:<source>`. Decisions do not read either, so they are exactly as before.
* **apply**: the old behaviour. It rewrites its own probabilities, move sizes, thesis priors and source credibility as results arrive.

Review what it has learned at any time (read-only), then decide:

    python3 -m paper_agent learning                      # status + every proposal beside the live value
    python3 -m paper_agent learning --promote            # dry run: what would go live
    python3 -m paper_agent learning --promote --apply    # make the reviewed proposals live (audited, logged in the strategy history)

The Intelligence tab shows the same status. Promote only after a review, and treat it as a new strategy version for the trial.

## 2. The strategy stamp
Every cycle records which strategy is in force. A new entry is added whenever a decision-affecting setting changes (the entry lists exactly which setting moved, old -> new), the strategy version
(`stamp.STRATEGY_VERSION`) changes, or learning is promoted. Operational settings (cycle times, memory limits, feed switches, spend caps, refresh intervals) never create an entry.
Any result can be attributed to the strategy in force when it was produced (`stamp.version_at`). The Intelligence tab shows the current version, fingerprint and number of changes.

## Rules during a trial
1. Do not change decision settings, the article-reader mode or thesis settings mid-trial; if you must, the stamp records it and the trial is split at that moment.
2. Code changes during a trial must pass `tools/golden.py check`: behaviour scenarios identical. Install outside the US session (02:30-09:00 NZDT).
3. A change to decision logic bumps `STRATEGY_VERSION` and a re-recorded baseline, and starts a new measurement period.

## Strategy S2 (build 37): what a market-wide shock hold means
S1 -> S2 changed one decision rule, found by reviewing the real open of Friday 30 Oct, where 8 queued BUYs (edge +2.3% to +5.2%) were held until they expired. SPY was genuinely down 1.37% that day, so the market gate worked; the
shock itself was mis-identified. A hold now needs: a story whose HEADLINE names a shock (the feed summary alone no longer makes a heist a bank run or an opinion column a war); at least `shock_min_sources` DISTINCT PUBLISHERS
(CNBC Top News + CNBC World are one); and the market falling by `shock_market_drop_pct`. The hold text now states the outlets and SPY's move. `risk.shock_applies_to` chooses whether a hold covers only BUYs decided before the story
(after_decision, the default, as before) or every queued BUY while the shock is live (all_buys). The missed-trade analysis files these under "held for a market-wide shock", so what the rule costs or saves can be read from results.
