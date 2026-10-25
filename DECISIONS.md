# Architecture Decisions

1. Virtual trading only; no broker integration.
2. Consider all verified US-listed equities.
3. Minimum BUY score is 65.
4. Every executed BUY is at least 20% of current portfolio equity.
5. Missing quotes become AWAITING_PRICE.
6. A 304 HTTP response is a healthy unchanged-source result.
7. Store timestamps in UTC; display Pacific/Auckland.
8. External endpoints must pass a live Orange Pi probe.
9. Failed sources use independent exponential backoff.
10. Pi-hole health is a deployment gate.
11. No standalone chat-only patches after repository creation.
12. Every deployment must originate from a tested, tagged commit.
