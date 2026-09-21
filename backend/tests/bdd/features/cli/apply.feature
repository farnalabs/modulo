Feature: Declarative Configuration CLI (`modulo apply`)
  As an operator
  I want to manage an org's schemas, model backends, pipelines and triggers
    from a single YAML file via `modulo apply -f <config.yaml>`
  So that infrastructure is applied declaratively and CI can gate on config
    drift with `--diff` (feat-apply)

  Background:
    Given a modulo apply test context

  # -- Config format & loading -------------------------------------------
  # (backend/src/modulo/cli/apply/loader.py + models.py)

  Scenario: A single-document config loads its declared entities
    Given an apply config declaring the schema "alpha"
    When I load the apply config
    Then the config carries the schema "alpha"

  Scenario: Multi-document YAML merges entities across documents
    Given an apply config whose first document declares the schema "alpha"
    And a second apply document declaring the model backend "openai"
    When I load the combined documents
    Then the config carries the schema "alpha"
    And the config carries the model backend "openai"

  Scenario: Duplicate names within one document are a load-time error
    Given an apply config declaring the schema "alpha" twice
    When I load the apply config
    Then the load fails with an error mentioning "duplicate schemas entity"

  Scenario: Duplicate names across documents are a load-time error
    Given an apply config whose first document declares the schema "dup"
    And a second apply document declaring the schema "dup"
    When I load the combined documents
    Then the load fails with an error mentioning "duplicate schemas entity across YAML documents"

  Scenario: An empty config file is rejected at load time
    Given an empty apply config file
    When I load the apply config
    Then the load fails with an error mentioning "is empty"

  Scenario: A trigger forward-referencing a pipeline in a later document is a load-time error
    Given an apply config whose first document declares only a trigger for pipeline "sample"
    And a second apply document declaring pipeline "sample"
    When I load the combined documents
    Then the load fails with an error mentioning "forward-references pipeline"

  # -- Secrets are refs-only ---------------------------------------------
  # (backend/src/modulo/cli/apply/models.py + executor.py resolve_secret_refs)

  Scenario: An inline api_key literal is rejected at validation
    Given an apply config declaring the model backend "openai" with the raw api_key "sk-live-123"
    When I load the apply config
    Then the load fails with an error mentioning "inline secret values are forbidden"

  Scenario: An env-ref api_key resolves when the variable is set
    Given an apply config declaring the model backend "openai" with the env api_key "${env:SK}"
    And the environment variable "SK" is "value-123"
    When I resolve secret refs
    Then the backend "openai" api_key resolves to "value-123"
    And nothing is blocked

  Scenario: A missing env-ref variable blocks the backend
    Given an apply config declaring the model backend "openai" with the env api_key "${env:SK}"
    When I resolve secret refs
    Then the backend "openai" is blocked mentioning "is not set"

  Scenario: A secretref:// api_key is blocked pending server-side resolution
    Given an apply config declaring the model backend "openai" with the api_key "secretref://kv/db"
    When I resolve secret refs
    Then the backend "openai" is blocked mentioning "not supported"

  # -- Planning ------------------------------------------------------------
  # (backend/src/modulo/cli/apply/plan.py)

  Scenario: An absent entity plans as created
    Given a schema entity "alpha" with description "Brand new"
    When I plan the entity
    Then the decision status is "created"

  Scenario: An identical entity plans as unchanged
    Given a schema entity "alpha" with description "Same"
    And the live schema "alpha" has description "Same"
    When I plan the entity
    Then the decision status is "unchanged"

  Scenario: A differing entity plans as updated
    Given a schema entity "alpha" with description "New"
    And the live schema "alpha" has description "Old"
    When I plan the entity
    Then the decision status is "updated"

  Scenario: A backend provider mismatch plans as blocked
    Given a model backend entity "openai" with provider "openai"
    And the live model backend "openai" has provider "anthropic"
    When I plan the entity
    Then the decision status is "blocked"
    And the decision reason mentions "provider mismatch"

  Scenario: A same-version schema version conflict plans as blocked
    Given a schema entity "alpha" with version "v1" whose content is {"a": 1}
    And the live schema "alpha" has version "v1" whose content is {"a": 2}
    When I plan the entity
    Then the decision status is "blocked"
    And the decision reason mentions "immutable"

  # -- Apply & verification -------------------------------------------------
  # (backend/src/modulo/cli/apply/executor.py — backend writes are verified)

  Scenario: A stored-but-broken backend credential is never reported as plain success
    Given an apply config declaring the model backend "openai" with the env api_key "${env:SK}"
    And the environment variable "SK" is "value-123"
    And the live org has no schemas and no model backends
    And the API accepts the backend create but its health check reports "unhealthy" with detail "401 from provider"
    When I run apply (not dry-run)
    Then the report contains "openai" in "failed"
    And the report has blockers
    And the failed entry error mentions "failed health check"
    And the API received the backend create request

  Scenario: Real apply is blocked when a secret ref cannot resolve
    Given an apply config declaring the model backend "openai" with the env api_key "${env:SK}"
    And the environment variable "SK" is missing
    And the live org has no schemas and no model backends
    When I run apply (not dry-run)
    Then the report contains "openai" in "blocked"
    And the report has blockers

  # -- Exit-code semantics ---------------------------------------------------
  # (dry-run/plan always exit 0; real apply exits 1 iff blocked/failed)

  Scenario: Dry-run returns a dry-run report with no blockers for a well-formed config
    Given an apply config declaring the schema "alpha"
    And an apply config declaring the model backend "openai" with the env api_key "${env:SK}"
    And the environment variable "SK" is "value-123"
    And the live org has no schemas and no model backends
    When I run apply as dry-run
    Then the report is a dry-run report
    And the report has no blockers

  Scenario: A report with any blocked entity is a blocker for exit-code purposes
    Given a plan report containing a blocked model backend "openai"
    Then the report has blockers
    And a report with only "unchanged" entries has no blockers

  # -- Drift mode (--diff) ----------------------------------------------------
  # (backend/src/modulo/cli/apply/drift.py — read-only, no write path)

  Scenario: Drift mode is a read-only report labelled drift
    Given an apply config declaring the schema "alpha"
    And the live org matches the config exactly for the schema "alpha"
    When I run apply in drift mode
    Then the report is labelled "drift"
    And the report is a read-only report with no write requests
    And the report has no drift

  Scenario: Declared-but-absent entities are drift
    Given an apply config declaring the schema "alpha"
    And the live org has no schemas and no model backends
    When I run apply in drift mode
    Then the report is labelled "drift"
    And the report has drift
    And the report lists "alpha" as "created"

  Scenario: Node-level drift detail breaks an updated pipeline graph down per element
    Given a desired pipeline "sample" whose graph adds the node "n1"
    And the live pipeline "sample" has a graph without the node "n1"
    When I build the drift detail
    Then the drift detail for pipeline "sample" has node additions ["n1"]

  Scenario: Drift rendering prefixes drift verbs and reports a drift summary
    Given a drift report containing a created schema "alpha"
    When I render the drift report
    Then the rendered table contains "drift create"
    And the rendered summary reads "drift summary"
