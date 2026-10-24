# FAR-2032: Branch Fixer single-attempt runbook

## Scope and ownership

This is an audit and execution runbook for the live Branch Fixer pipeline (`6bdf5fca-1b29-4ad2-a273-2b3684e97348`). The graph and its masked credentials are deployment state stored outside this repository; the repository contains no source-of-truth copy of the live `agent_commands` value.

**This runbook does not mutate the live graph.** The Conductor must review the evidence, perform the irreversible graph write through the Modulo MCP graph-update operation, and perform the post-write verification described below.

## Deployment guard evidence

The sequencing guard is deployed before this configuration change:

- Guard PR: `#2131`
- Guard commit: `4b96a243c3990053ded4cfe895655468b9182376`
- Observed repository evidence: `git show --no-patch --format='%H %s' 4b96a243c3990053ded4cfe895655468b9182376` returned:
  `4b96a243c3990053ded4cfe895655468b9182376 fix(FAR-2025): stop tripling the cost of over-budget runs (#2131)`
- Observed deployment evidence: `GET https://app.modulo.run/healthz/ready` returned HTTP success with `"status":"ok"`, `"version":"0.3.323"`, and `checks.migrations.status="ok"` / `checks.migrations.detail="migrations up to date"`. The same response reported the database, Redis, checkpointer, workers, dispatcher reconciliation, stale-run recovery, and break-glass checks healthy.

The live graph was read through the Modulo MCP graph-read operation. It is not represented in this repository, so the live read is the authoritative current-state observation.

## Current live graph and exact intended diff

The live `sandbox_agent` node is:

- Pipeline: `6bdf5fca-1b29-4ad2-a273-2b3684e97348`
- Node: `82ca2b0f-1c50-4a5c-8efc-a81cb3ae60b8`
- `template_id`: `modulo-opencode`
- `timeout_seconds`: `3300`
- `env_vars.GITHUB_TOKEN`: rendered as the six-character mask `••••••`

The current non-conflict branch is the `else` block below. This is the only block to replace; all commands, comments, fields, node settings, and masked values outside this block must be preserved byte-for-byte.

### Current non-conflict ladder excerpt

```bash
else
  # Default (CI-failure) task profile: budget-aware 3-attempt ladder, unchanged.
  for ATTEMPT in 1 2 3; do
    REMAINING=$(( BF_BUDGET - SECONDS ))
    case $ATTEMPT in 1) MODEL=$M1; TMO=$(( REMAINING * 2 / 3 )) ;; 2) MODEL=$M2; TMO=$(( REMAINING / 2 )) ;; 3) MODEL=$M3; TMO=$REMAINING ;; esac
    if [ "$TMO" -lt 60 ]; then echo "branch-fixer: only ${TMO}s of budget left - not starting attempt $ATTEMPT"; break; fi
    echo "branch-fixer: attempt $ATTEMPT of 3 with $MODEL (timeout ${TMO}s, ${REMAINING}s of budget left)"
    OPENCODE_API_KEY="$APP_MODULO_OPENCODE_API_KEY" GITHUB_TOKEN="$GITHUB_TOKEN" timeout --kill-after=30 "$TMO" opencode run --model "$MODEL" --auto --format json < /home/user/prompt.md 2>&1 | tee /tmp/opencode.out
    if [ -f /home/user/output.json ]; then
      MODEL_USED="$MODEL" python3 -c 'import json,os; p="/home/user/output.json"; d=json.load(open(p)); d["model_used"]=os.environ.get("MODEL_USED"); json.dump(d,open(p,"w"))' 2>/dev/null || true
      break
    fi
    echo "branch-fixer: attempt $ATTEMPT produced no output.json - retrying"
  done
fi
```

### Exact intended replacement

Replace only the current `else` ladder body with this single-attempt body. The closing `fi` remains unchanged.

```bash
else
  # Default (CI-failure) task profile: exactly one budget-aware long attempt.
  REMAINING=$(( BF_BUDGET - SECONDS ))
  MODEL=$M1
  TMO=$(( REMAINING - 120 ))
  echo "branch-fixer: default task - single attempt with $MODEL (timeout ${TMO}s, ${REMAINING}s of budget left)"
  OPENCODE_API_KEY="$APP_MODULO_OPENCODE_API_KEY" GITHUB_TOKEN="$GITHUB_TOKEN" timeout --kill-after=30 "$TMO" opencode run --model "$MODEL" --auto --format json < /home/user/prompt.md 2>&1 | tee /tmp/opencode.out
  if [ -f /home/user/output.json ]; then
    MODEL_USED="$MODEL" python3 -c 'import json,os; p="/home/user/output.json"; d=json.load(open(p)); d["model_used"]=os.environ.get("MODEL_USED"); json.dump(d,open(p,"w"))' 2>/dev/null || true
  fi
fi
```

The resulting graph diff must show no change to the conflict branch, the pre-branch setup, the output fallback, the cost merge, node metadata, or any credential-bearing field. In particular, do not change `GITHUB_TOKEN` from the mask or re-enter its value.

## Mask-preservation gate

The graph response must be read and saved as UTF-8. Do not use Windows PowerShell 5.1 `Get-Content -Raw` for a read-modify-write operation.

Before submitting the graph update, extract every masked field that will be sent back and abort unless each mask satisfies all of these assertions:

```python
mask = node["env_vars"]["GITHUB_TOKEN"]
assert len(mask) == 6
assert all(ord(ch) == 0x2022 for ch in mask)
assert mask == "••••••"
```

Record the pre-write assertion as evidence: length `6`; codepoints `[U+2022, U+2022, U+2022, U+2022, U+2022, U+2022]`; strict value `••••••`.

After the write, read the live graph back through MCP and run the same assertions against the returned `GITHUB_TOKEN` mask. Also assert strict equality against the exact pre-write value:

```python
before_mask = "••••••"  # captured from the UTF-8 graph response, not reconstructed from mojibake
# ... perform the Conductor-authorized graph write ...
after_mask = read_back_node["env_vars"]["GITHUB_TOKEN"]
assert len(after_mask) == 6
assert all(ord(ch) == 0x2022 for ch in after_mask)
assert after_mask == before_mask
```

If any length, codepoint, or strict-equality assertion fails, stop. Do not retry the write and do not “repair” the secret by sending a guessed value.

## Write and read-back procedure

1. Re-read the live graph immediately before mutation and confirm the pipeline ID, node ID, current ladder excerpt, `timeout_seconds=3300`, and the six-character mask.
2. Save the graph response using an explicit UTF-8 decoder. Preserve all fields from that response unchanged except the specified non-conflict ladder block.
3. Run the mask-preservation assertions above and print their raw results before the write.
4. Produce a structural diff of the serialized node payload and verify that the only command change is the ladder-to-single-attempt replacement. The diff must contain no changes to `env_vars`, node metadata, or any other field.
5. Obtain Conductor authorization, then submit the complete graph through the graph-update operation. This runbook itself does not submit it.
6. Read the graph back immediately. Verify the exact intended replacement is present, the conflict branch remains unchanged, and the mask is strict-equal to the pre-write mask.
7. Store the before/after graph excerpts, diff, mask assertions, read-back result, timestamp, and the operator/run identifier in the delivery evidence. A successful HTTP/MCP response alone is not sufficient.

## Real-dispatch verification

After read-back succeeds, dispatch a real Branch Fixer run with a normal non-conflict CI-failure payload. Use a disposable test PR or a controlled failure that is safe for the dogfood repository; do not use a production customer workflow.

Verify from the run's retained status/output evidence, not only the final summary:

- The run uses the updated Branch Fixer snapshot. Record the `snapshot_id` and compare it with the graph read used for the write.
- The node output/log contains exactly one default-profile attempt line: `branch-fixer: default task - single attempt ...`.
- There is no `attempt 2 of 3`, `attempt 3 of 3`, or `produced no output.json - retrying` line.
- There is exactly one invocation of the default-profile `opencode run` and at most one `output.json` post-processing event.
- The run completes or fails once; it must not re-enter the in-node ladder. If the run-level guard causes an executor-level re-dispatch after a node failure, record that separately and distinguish it from the removed in-node retry loop.
- Capture the run ID, node attempt count, relevant log tail, terminal status, error code if any, and the final graph read-back.

A real dispatch that cannot prove the snapshot and one-attempt log shape is inconclusive and must not be reported as verification.

## Rollback

If the post-write read-back or real-dispatch verification fails, stop further dispatches and restore the exact pre-write graph payload through the same authorized graph-update path. Reuse the UTF-8-captured pre-write graph, not a hand-reconstructed command or secret. Before rollback, assert that the pre-write mask is still exactly six U+2022 codepoints; after rollback, read back the graph and assert strict equality again.

If the mask was ever written with a value that fails the strict-equality check, do not guess or overwrite it. Escalate immediately for credential rotation and inspect the connector/secret audit trail. Record the failed write, affected node, timestamp, and response.

After rollback, run one safe read-only graph check and do not dispatch again until the Conductor confirms the graph and credential mask are correct. The rollback restores the prior three-attempt non-conflict ladder; it does not alter the deployed FAR-2025 guard.

## Non-mutation statement

This document records the observed live graph and the exact minimal replacement, but it does not itself mutate the live graph. The Conductor owns authorization, the live write, read-back verification, real-dispatch verification, and rollback if required.
