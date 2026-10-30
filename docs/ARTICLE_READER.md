# Reading the full article (no LLM)

Headlines mislead; the numbers are in the article. From build 32 the bot downloads the page behind the news items worth reading and understands it with an in-house, rule-based engine (`lens.py`),
then folds what it found into the headline's reading BEFORE any gate looks at the story. No LLM, no API cost, deterministic, fast (about half a millisecond per article to analyse).

## What it extracts (each fact keeps the one source sentence, so you can check it)
Earnings per share and revenue **versus the estimate** (with the surprise %), guidance raised / cut / kept (with the size of the change when given), deals (agreed vs "in talks", premium, size), contracts and buybacks
(sized against the company's market value), dividend changes, analyst upgrades / downgrades / price targets, and adverse or favourable events (bankruptcy, delisting, restatement, investigations, lawsuits, recalls,
FDA outcomes, short-seller reports, cyber incidents, executive exits, strikes / outages, record results, partnerships, insider trades).
It understands negation ("did not miss"), hedging ("in talks", "could", "reportedly"), attribution ("an analyst said"), history ("last year"), losses as negative numbers (a narrower loss is a beat), and the difference between
news, a press release, an opinion piece, a list article, sponsored content and a call transcript. It notices when the lead says the news happened days ago, and which company an article is actually about.

## Modes (Settings -> Article reader)
| mode | what the article may do |
|---|---|
| off | nothing: headlines and snippets only |
| shadow | read and record only; changes nothing (use it to study) |
| **guard** (default) | CANCEL or WEAKEN a candidate: the full text contradicts the headline, it is opinion / sponsored / a transcript, it is old news, the company is only mentioned in passing, signals are mixed. It can never create or enlarge a candidate. |
| full | guard, plus: confirm and strengthen a candidate, or rescue one whose headline was too vague ("X Reports Third Quarter Results") from the article's own numbers |

New funnel reasons: `article_disagrees`, `stale_in_article`. The Decisions -> "Why?" page has a section **What the full article says** (the numbers, the sentence they came from, what it changed). The Funnel shows
"articles read: N (full page X, feed snippet only Y)" and the sites being left alone. Preflight warns if no page ever loads.

## How it behaves on the internet
Public web addresses only (a link can never make the Pi probe your home network; every redirect is re-checked); http/https on ports 80/443; **robots.txt is honoured**; an honest User-Agent with your contact email;
1.5 MB and 8 s limits; at most 8 articles and 25 seconds per cycle (best-ranked first: earnings, outlook, deals, ratings...); a site that keeps refusing us is left alone for 24 hours; social / video / SEC / government pages are never fetched.
Paywalled pages are not bypassed: the wall text is dropped, a teaser's real lead is kept, and a wall-only page falls back to the feed snippet.
Only the extracted facts are stored (about 3 KB per article), never the article text. They are cleaned up with the news (Settings -> Database).

## Honest limits
It is narrower than an LLM: it only reports what it can verify with patterns, and says nothing about the rest. Many sites will refuse or paywall us, so a share of articles will be read from the feed snippet only. On your
real snippets (median 88 characters) it found facts in 12% of items and would have changed none of 64 real BUY-rated stories: the value depends on full pages being readable. Judge it on the Funnel line, the Why pages and,
once the first predictions are scored, the results: guard mode first, `full` when you trust it. When you turn the LLM on, run it in shadow beside this reader and compare.
