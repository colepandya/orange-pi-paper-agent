# Relationship graph (build 39)

An event at one company becomes a smaller, discounted idea at the companies it affects. Rules and data only; no LLM.

Edges: `config/relations.json` (hand-written supplier / customer / competitor / peer links; 41 peer groups and 67 directed links to start; edit it in /etc/paper-agent/relations.json, the shipped copy is relations.default.json) and measured co-movement (60-session correlation >= 0.75, refreshed daily, strongest 4 per stock). The most specific relationship wins (a rival in the same peer group is a competitor, not both).

How an event spreads (graph.py FACTORS, derived direction = source direction x factor):
supplier +0.6 (a customer's demand news reaches its suppliers) | competitor -0.35 | peer +0.35 | co-mover +0.30 | customer 0, except supply-side shocks (production_disruption: +0.5 to customers too) | regulatory news spreads to peers (+0.6) | company-only news (insider, analyst, leadership, buyback, capital raise) never spreads.
Only company news with magnitude >= 0.45, confidence >= 0.6 and |direction| >= 0.3 spreads, at most 4 ideas per event and 12 per cycle, only to stocks in the universe, never an idea that is itself derived, each discounted (graph.derived_discount) and tagged `graph:<role>` so outcomes can be scored per relationship kind.
Derived ideas then pass exactly the same gates, sizing and exits as any other idea.

Also: the correlation gate. A new position that moves with a holding on the SAME side is shrunk above risk.corr_haircut_from (0.6) and refused at risk.corr_block (0.92). A correlated hedge on the other side is never penalised.
