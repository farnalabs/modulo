Feature: HITL Approval Gate
  As a pipeline author
  I want to pause a run at a defined gate and require human approval
  So that humans remain in control of critical decisions

  @deprecated
  Scenario: All HITL approval gate scenarios have been migrated to tests/features/hitl/
    See: claim.feature, approve.feature, reject.feature, overdue_warning.feature

  # This feature file is kept only for reference.
  #
  # All scenarios have been migrated to the working feature files under
  # tests/features/hitl/:
  #
  #   - Run pauses at approval gate         → claim.feature (Background + Scenario)
  #   - Approved run resumes                → approve.feature "Approve a claimed gate"
  #   - Rejected run stops                  → reject.feature
  #   - Gate times out if not actioned      → overdue_warning.feature
  #   - Non-approver cannot approve         → approve.feature "Approve a gate claimed by another user"
  #
  # The deprecated scenarios used:
  #   - Old API path /api/runs/{run_id}/approve (actual: /api/v1/runs/{run_id}/hitl/{gate_id}/approve)
  #   - No claim_token in request body (claim_token is now required)
  #   - "waiting_for_approval" status (actual: "awaiting_human")
  #   - Non-terminal "rejected" status (actual flow: rejected resumes graph with router)
  #
  # Do not add new scenarios here. Use tests/features/hitl/ instead.
