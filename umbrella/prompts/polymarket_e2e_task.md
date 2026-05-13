# Polymarket Workspace E2E Task

Build a workspace system that researches public Polymarket markets resolving soon and produces a simulation-based shortlist.

This is an engineering validation task, not betting advice. The output must label itself as a simulation and must not present a trade as guaranteed.

Minimum deliverables:

1. A live data collection path for candidate markets and public prices from public Polymarket data or documented public endpoints. Do not create or use mock data, stubs, or fallback fake markets. If live collection is blocked, record the blocker with evidence instead of substituting fake data.
2. A scoring model for soon-resolving markets that combines time-to-resolution, liquidity, price, uncertainty, and evidence freshness.
3. A simulation or backtest-style script that can run without manual steps.
4. A report artifact with assumptions, excluded markets, risks, and the top candidate from the simulation.
5. Tests or smoke checks that prove the pipeline runs end to end.
6. A local git commit for the workspace changes, no push.

If endpoint behavior or market data format is unclear, research it with `web_search`, `browse_page`, or HTTP commands from the workspace. Do not finish with a mock-only implementation while live public data collection is still reasonably implementable.

If you implement agents or multi-agent orchestration, retrieve GMAS context first with `get_gmas_context`.

When this task is wrapped by the standard Umbrella workspace mission, the full long-run completion contract from that wrapper still applies.
