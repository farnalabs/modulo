---
title: Eval Regression Alerts
description: Detect significant pass-rate drops for eval definitions by comparing recent results against a baseline window.
---

# Eval Regression Alerts

Modulo's eval regression detection compares pass rates between a recent window and a baseline window for each eval definition, flagging significant drops as alerts.

Regression alerts are available via the `GET /api/v1/admin/evals/regressions` endpoint (admin-only). The response includes a list of alerts with `eval_id`, `eval_name`, `prev_pass_rate`, `current_pass_rate`, `drop_pct`, `trend` (declining / stable / improving), and `affected_run_ids`.

For the full specification, see the [PRD §8.17](/prd#8-17-eval-regression-alerts).
