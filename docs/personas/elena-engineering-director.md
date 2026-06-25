# Persona: Elena — VP / Director of Engineering

| Attribute | Value |
|---|---|
| **Role** | VP Engineering / Engineering Director |
| **Org size** | 50–300 engineers across 5–15 teams |
| **Technical level** | Technical leader; code-adjacent, reads dashboards not diffs |
| **Industry** | B2B SaaS, marketplace |
| **Compliance** | SOC 2, GDPR |
| **Budget authority** | Primary decision-maker for platform investments |

## Goals

- Ship faster without sacrificing quality — prove the ROI of agentic delivery in quarterly business reviews
- See which teams are adopting agentic workflows and whether it's making them more productive
- Control spend: understand token costs per team, per pipeline, per run
- Make data-driven decisions about where to increase agent autonomy vs. keep human oversight
- Give directors and leads visibility into pipeline health without flooding them with detail

## Pain points / triggers

- Board is asking "what are we doing about AI in engineering?"; needs a coherent answer with metrics
- Some teams are already using random AI coding tools with no consistency, no governance, no measurement
- Current velocity metrics (PRs merged, cycle time) don't capture whether AI is helping or adding noise
- Can't tell which parts of the SDLC are bottlenecks because there's no unified pipeline view
- Worried about "productivity theatre" — lots of AI activity, no improvement in shipped quality

## Key scenarios that must work

1. **`@persona-elena`** `features/observability/metrics.feature` — Org-wide dashboard: runs/time, avg cycle time, eval pass rate by team
2. **`@persona-elena`** `features/eval/eval_dashboard.feature` — Quality score trend per pipeline; regression alerts
3. **`@persona-elena`** `features/pipelines/run_variants.feature` — Side-by-side eval comparison: is the new GPT model actually better?
4. **`@persona-elena`** `features/pipelines/scheduling.feature` — Scheduled quality reports generated and posted to Slack weekly
5. **`@persona-elena`** `features/eval/complexity_reviewer.feature` — Complexity reviewer flags when a pipeline is getting fragile
6. **`@persona-elena`** `features/eval/feedback_system.feature` — Track how often human reviewers reject agent output; trend over time
7. **`@persona-elena`** `features/observability/run_logs.feature` — Drill from dashboard into a single run to understand a quality dip
8. **`@persona-elena`** `features/eval/eval_suite_crud.feature` — Create eval suites aligned with team OKRs; share across org
9. **`@persona-elena`** `features/pipelines/run_lifecycle.feature` — See all runs (active, completed, failed, cancelled) in one place

## Anti-scenarios (must NOT require)

- Configuring individual pipelines or schemas — that's DevX/platform's job
- Reading a YAML file or editing a pipeline node to get a dashboard
- Understanding LangGraph internals to interpret run results

## What success looks like

Elena opens the Modulo dashboard on Monday morning. Team Alpha's eval pass rate is flat at 94% (stable). Team Beta's pipeline has a complexity warning — she checks and sees they added 3 unstructured agent nodes. She pings the platform team to schedule a refactor. Token spend is within budget. She copies the Q3 eval comparison chart into her board deck. No surprises.
