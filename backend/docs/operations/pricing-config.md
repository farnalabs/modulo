# Pricing Configuration

Modulo tracks token usage and costs for each model backend invocation.
This document explains how pricing is configured and how to add new models.

## How Pricing Works

- Each model backend has a `cost_tracking` field (`enabled`/`disabled`) — see `ModelBackend.cost_tracking` in `src/modulo/db/models/model_backend.py`
- Token counts are recorded per node execution and costs are calculated using `get_pricing()` in `src/modulo/core/pricing.py`
- Costs are displayed in the run detail view and the admin cost dashboard at `GET /api/v1/admin/costs`
- Spend limits can be set per org and per team via `PUT /api/v1/admin/costs/limits/org` and `PUT /api/v1/admin/costs/limits/teams/{team_id}`
- Spend anomalies are detected daily (org spend > 2x rolling 7-day average) at `GET /api/v1/admin/costs/anomalies`

## Pricing Table

The `PRICING_TABLE` in `src/modulo/core/pricing.py` maps model provider/pattern pairs to per-token costs.
Lookup uses `fnmatch.fnmatch` — the first matching entry wins, so list specific patterns before generic ones.

| Provider | Model Pattern | Input Price/1K | Output Price/1K |
|---|---|---|---|
| openai | gpt-4o-mini | $0.15 | $0.60 |
| openai | gpt-4o-mini* | $0.15 | $0.60 |
| openai | gpt-4o | $2.50 | $10.00 |
| openai | gpt-4o* | $2.50 | $10.00 |
| openai | o3 | $10.00 | $40.00 |
| openai | o3* | $10.00 | $40.00 |
| openai | o4-mini | $1.10 | $4.40 |
| openai | o4-mini* | $1.10 | $4.40 |
| anthropic | claude-sonnet-4 | $3.00 | $15.00 |
| anthropic | claude-sonnet-4* | $3.00 | $15.00 |
| anthropic | claude-sonnet-4.5 | $3.00 | $15.00 |
| anthropic | claude-sonnet-4.5* | $3.00 | $15.00 |
| anthropic | claude-haiku-3.5 | $0.80 | $4.00 |
| anthropic | claude-haiku-3.5* | $0.80 | $4.00 |
| groq | * | $0.00 | $0.00 |
| deepseek | deepseek-chat | $0.27 | $1.10 |
| deepseek | deepseek-reasoner | $0.55 | $2.19 |
| deepseek | deepseek-v3 | $0.27 | $1.10 |
| deepseek | deepseek-r1 | $0.55 | $2.19 |
| perplexity | sonar-reasoning* | $1.00 | $5.00 |
| perplexity | sonar-pro* | $3.00 | $3.00 |
| perplexity | sonar* | $1.00 | $1.00 |
| togetherai | mistral* | $0.60 | $0.60 |
| togetherai | mixtral* | $0.60 | $0.60 |
| togetherai | llama* | $0.80 | $0.80 |
| togetherai | Llama* | $0.80 | $0.80 |
| azure_openai | gpt-4o* | $2.50 | $10.00 |
| azure_openai | gpt-4o-mini* | $0.15 | $0.60 |
| azure_openai | o3* | $10.00 | $40.00 |
| azure_openai | o4-mini* | $1.10 | $4.40 |

## Adding a New Model

1. Add a `PricingConfig` entry to `PRICING_TABLE` in `src/modulo/core/pricing.py`
2. The first matching pattern wins — add specific patterns (e.g. `"gpt-4o"`) before generic ones (e.g. `"gpt-4o*"`)
3. Run tests: `python -m pytest tests/unit/core/test_pricing.py`

### Rules

- Provider string must match exactly what the model backend uses (see `ModelBackendProvider` enum)
- Pattern uses glob syntax; `"*"` matches everything (useful for free-tier catch-alls)
- Prices are in USD per 1,000 tokens by default; set `currency` on `PricingConfig` to override
- Zero prices are valid (e.g. Groq free tier), but non-Groq entries must have positive prices (enforced by test)

## Environment Variables

- `MODULO_COST_CURRENCY`: Default currency for cost display (default: `"USD"`)

## Key Files

| File | Purpose |
|---|---|
| `src/modulo/core/pricing.py` | Pricing table and `get_pricing()` lookup |
| `src/modulo/db/models/model_backend.py` | `ModelBackend.cost_tracking` and `currency` fields |
| `src/modulo/api/routes/costs.py` | Cost report, spend limits, anomaly detection, CSV export, scheduled reports |
| `tests/unit/core/test_pricing.py` | Tests for pricing lookup and table integrity |
