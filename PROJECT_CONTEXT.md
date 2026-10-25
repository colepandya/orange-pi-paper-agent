# Project Context

## Objective

A fully virtual, news-driven US equity trading experiment running on an
Orange Pi Zero 3. The application has no brokerage connection and never
places real orders.

## Experiment

- Starting portfolio: USD 1,000,000
- Universe: verified US-listed equities
- Holding horizon: 1 to 15 trading days
- Minimum entry score: 65
- Score 65–79: target 20% of current equity
- Score 80–89: target 25%
- Score 90–94: target 35%
- Score 95+: target 50%
- Minimum executed buy: 20% of current equity
- Maximum single stock: 50%
- Maximum theme: 80%
- Maximum deployed: 90%
- Hard loss boundary: 12%
- No real-money transactions

## Price handling

Quote order:

1. Yahoo chart JSON
2. Stooq public page
3. Google Finance page fallback
4. Cached quote no older than 24 hours

A qualifying decision without a price becomes AWAITING_PRICE and
is retried. Prices are never invented.

## Time

Database timestamps remain UTC.
Dashboard timestamps display Pacific/Auckland with automatic NZST/NZDT.

## Infrastructure

Pi-hole shares the Orange Pi and must remain healthy.
Collector CPU and memory limits must remain enforced.
