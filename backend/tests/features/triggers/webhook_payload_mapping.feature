Feature: Webhook Payload Mapping
  As a pipeline operator
  I want to map incoming webhook payload fields to run_context
  So that external event data is available to pipeline agents

  Background:
    Given I am authenticated as an admin in org "acme"

  Scenario: Simple field mapping
    Given org "acme" has pipeline "mapped-pipeline" with payload mapping {"branch": "$.ref"}
    When I POST /api/webhooks/mapped-pipeline with payload {"ref": "refs/heads/main"} and valid HMAC
    Then the run has run_context with branch "refs/heads/main"

  Scenario: Nested field extraction
    Given org "acme" has pipeline "mapped-pipeline" with payload mapping {"pr_number": "$.pull_request.number"}
    When I POST /api/webhooks/mapped-pipeline with payload {"pull_request": {"number": 42}} and valid HMAC
    Then the run has run_context with pr_number 42

  Scenario: Missing mapped field uses default
    Given org "acme" has pipeline "mapped-pipeline" with payload mapping {"branch": "$.ref", "default_branch": "main"}
    When I POST /api/webhooks/mapped-pipeline with payload {} and valid HMAC
    Then the run has run_context with branch "main"

  Scenario: Complex transformation via template
    Given org "acme" has pipeline "mapped-pipeline" with payload mapping {"title": "PR: {{$.pull_request.title}}"}
    When I POST /api/webhooks/mapped-pipeline with payload {"pull_request": {"title": "Fix bug"}} and valid HMAC
    Then the run has run_context with title "PR: Fix bug"
