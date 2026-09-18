# Modulo Grafana Dashboards

Pre-built Grafana dashboards for Modulo's OpenTelemetry metrics. These provide observability into pipeline performance, human-in-the-loop review workflow, and LLM cost tracking.

## Prerequisites

- **Grafana 9+** (tested with 9.5 / 10.x)
- **Prometheus** or **Grafana Mimir** as a data source, receiving OTel metrics from Modulo's OpenTelemetry exporter
- The Modulo OTel metrics pipeline must be active and exporting to the Prometheus endpoint

## Import Instructions

### Via Grafana UI

1. Open your Grafana instance and sign in
2. Click the **Connections** icon (⚡) in the left sidebar → **Data Sources** → verify your Prometheus data source is configured
3. Click the **Dashboards** icon (📊) → **New** → **Import**
4. In the **Import via dashboard JSON model** box, paste the contents of the JSON file, or drag the file into the upload area
5. Click **Load**
6. On the next screen, select your Prometheus data source from the dropdown
7. Click **Import**

### Via Grafana API

```bash
curl -X POST "http://admin:admin@localhost:3000/api/dashboards/db" \
  -H "Content-Type: application/json" \
  -d @pipeline-performance.json
```

## Dashboards

| File | Description | Key Metrics |
|---|---|---|
| `pipeline-performance.json` | Pipeline run durations, volumes, error rates, and slowest nodes | p50/p95/p99 latency, runs/hour, error %, active runs, node-level timing |
| `hitl-review.json` | HITL gate activity, review speed, approval rates, and claim token expiry | Gates/day, avg review time, approval %, pending gates, claimed vs expired tokens |
| `cost-tracking.json` | LLM spend by org/model/pipeline, token volume, and cost forecasting | USD spend, token rates, monthly projections |

## Required OTel Attributes

The dashboards query the following span attributes and metric labels. Your OTel exporter must include these:

### Pipeline Performance

| Attribute / Label | Appears On | Used In |
|---|---|---|
| `span_name = "pipeline.run"` | Duration histogram | Run duration panels |
| `span_name = "node.execute"` | Node duration histogram | Slowest nodes panel |
| `pipeline_name` | All pipeline metrics | Variable filter, grouping |
| `pipeline.id` | Run tracking spans | Run identification |
| `run.id` | Run tracking spans | Run identification |
| `status` (`"ok"` / `"error"`) | `modulo_runs_total` | Error rate calculation |

Expected metrics:
- `modulo_pipeline_duration_seconds_{bucket,count,sum}` – histogram
- `modulo_node_duration_seconds_{bucket,count,sum}` – histogram
- `modulo_runs_total` – counter (labels: `pipeline_name`, `status`)
- `modulo_runs_active` – gauge

### HITL Review

| Attribute / Label | Appears On | Used In |
|---|---|---|
| `hitl.gate_id` | HITL spans | Gate identification |
| `hitl_status` (`"reached"`, `"approved"`, `"rejected"`, `"expired"`) | `modulo_hitl_gates_total` | Gate state filtering |
| `hitl.claimed_by` | Claim spans | Reviewer attribution |
| `pipeline_name` | All HITL metrics | Variable filter, grouping |
| `status` | Claim token metrics | Claimed vs expired |

Expected metrics:
- `modulo_hitl_gates_total` – counter (labels: `pipeline_name`, `hitl_status`)
- `modulo_hitl_review_time_seconds_{sum,count}` – histogram
- `modulo_hitl_gates_active` – gauge
- `modulo_hitl_claim_tokens_total` – counter (labels: `pipeline_name`, `status`)

### Cost Tracking

| Attribute / Label | Appears On | Used In |
|---|---|---|
| `cost_org_id` | Cost metrics | Variable filter, org breakdown |
| `cost_pipeline_id` | Cost metrics | Variable filter, pipeline breakdown |
| `cost_model_id` | Cost metrics | Variable filter, model breakdown |
| `llm_type` (`"input"` / `"output"`) | Token metrics | Input vs output split |

Expected metrics:
- `modulo_cost_usd_total` – counter (labels: `cost_org_id`, `cost_pipeline_id`, `cost_model_id`)
- `modulo_llm_tokens_total` – counter (labels: `cost_org_id`, `cost_pipeline_id`, `cost_model_id`, `llm_type`)

## Dashboard Variables

### Pipeline Performance

| Variable | Type | Source | Purpose |
|---|---|---|---|
| `datasource` | Data source | Prometheus | Select your Prometheus backend |
| `pipeline` | Query result | `label_values(...)` | Filter by pipeline name |

### HITL Review

| Variable | Type | Source | Purpose |
|---|---|---|---|
| `datasource` | Data source | Prometheus | Select your Prometheus backend |
| `pipeline` | Query result | `label_values(...)` | Filter by pipeline name |

### Cost Tracking

| Variable | Type | Source | Purpose |
|---|---|---|---|
| `datasource` | Data source | Prometheus | Select your Prometheus backend |
| `org` | Query result | `label_values(...)` | Filter by organisation ID |
| `pipeline` | Query result (dependent on `$org`) | `label_values(...)` | Filter by pipeline ID |
| `model` | Query result (dependent on `$org`, `$pipeline`) | `label_values(...)` | Filter by model ID |

## Customization

### Adding a new panel

1. Open the dashboard in Grafana
2. Click **Add panel** → choose visualization type
3. Write a PromQL query using the expected metrics listed above
4. Reference dashboard variables with `$variable_name` syntax

### Changing the time range

Update the `"time"` block at the top of the JSON:

```json
"time": {
  "from": "now-7d",
  "to": "now"
}
```

### Adding a new variable

```json
{
  "name": "status",
  "type": "query",
  "query": "label_values(modulo_runs_total{...}, status)",
  "includeAll": true,
  "multi": true
}
```

### Adapting to a different data source

Replace `"type": "prometheus"` with your data source type (e.g. `"mimir"`, `"victoriametrics"`) in:
1. The `datasource` variable
2. Each panel's `datasource` block

### Metric prefix

If your OTel collector uses a different metric prefix (e.g. `acme_modulo_` instead of `modulo_`), replace all occurrences.

## Notes

- All dashboards default to a 30-second auto-refresh and a 24-hour window
- The Claim Token Expiry panel uses a stacked time-series bar chart (not a native calendar heatmap) – install the [Discrete](https://grafana.com/grafana/plugins/natel-discrete-panel/) or [Status History](https://grafana.com/grafana/plugins/marcusolsson-status-history-panel/) panel plugin for a true calendar heatmap view
- Cost dashboards use `increase(...[$__interval])` which works with Prometheus counter metrics – ensure your metrics are counters, not gauges
