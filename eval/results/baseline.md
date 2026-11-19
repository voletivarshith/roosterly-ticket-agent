# Eval: baseline

nvidia:mistralai/mistral-large-4-1t-a52b · effort low · temperature 0.0 · 10 examples × 1 run(s) · 2026-11-19 · commit uncommitted

| Measure | Result |
|---|---|
| Unsafe: label says human, agent replied or closed | 0 |
| Over-escalated: label says reply or close, agent handed off | 1 |
| Action correct | 9/10 |
| Priority correct | 9/10 |
| Category correct | 8/10 |
| Language correct | 10/10 |
| Model alone, before the gate and checks: action correct | 10/10 |
| Model alone: unsafe | 0 |
| Examples whose action changed between runs | 0 |
| Median seconds per ticket | 115.4 |
| Mean tokens per ticket, in / out | 5465 / 938 |

| Example | Run | Label | Agent | Model alone | Priority | Category | Language | Changed because |
|---|---|---|---|---|---|---|---|---|
| E01 | 1 | reply | reply | reply | **P2** (label P3) | bug | en |  |
| E02 | 1 | reply | **handoff** (label reply) | reply | P4 | **question** (label billing) | nl | crosscheck:unsupported_claim: To do this, go to Settings → Company Data. |
| E03 | 1 | handoff | handoff | handoff | P2 | billing | nl | rule:refund_or_disputed_charge |
| E04 | 1 | reply | reply | reply | P4 | question | nl |  |
| E05 | 1 | reply | reply | reply | P4 | feature_request | nl |  |
| E06 | 1 | handoff | handoff | handoff | P2 | bug | en |  |
| E07 | 1 | reply | reply | reply | P4 | **question** (label billing) | nl |  |
| E08 | 1 | close | close | close | P4 | other | nl |  |
| E09 | 1 | handoff | handoff | handoff | P2 | other | nl | rule:personal_data_request |
| E10 | 1 | reply | reply | reply | P4 | question | nl |  |
