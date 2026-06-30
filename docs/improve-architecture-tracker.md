# improve-architecture Tracker

Current index: 3
Last updated: 2026-06-30T10:00:00Z

## In-flight
- feat-core-pipeline-execution → complete

## History
- 2026-06-30: feat-auth-scim → complete, cross-cutting QA: marked 34 behaviours [x], added bdd/unit-tests frontmatter, fixed 403→401 claim, added 3 new gaps
- 2026-06-30: feat-core-pipeline-execution → complete, cross-cutting QA: fixed broken frontmatter (missing bdd:), marked 5 behaviours [x] and 1 [ ], added 13 new behaviours from error path audit, created unit test file (discovered missing), added 4 BDD scenarios (empty pipeline, node returns None, runaway protection, output rejection), added 4 new known gaps (retry not implemented, DB connection lost, checkpoint migration, raised OTel verify)
- 2026-06-30: feat-evals-system → complete, cross-cutting QA: fixed broken YAML (missing bdd:), marked 24 [ ] → [x], added 20 new behaviour checkboxes from error path audit, created test_eval_engine.py (53 unit tests), fixed 3 code bugs (regex invalid pattern, LLM judge non-numeric score, regex flags support), added 3 new known gaps (JMESPath syntax errors, $ref resolution, schema depth limit), created website stub
