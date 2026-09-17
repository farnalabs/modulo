Feature: Router Decision Nodes
  As a pipeline author
  I want first-class Router nodes that route execution on runtime state
  So that pipeline flow adapts without hand-wired conditional edges

  Scenario: Router authors as a first-class graph node type
    Given a pipeline graph with entry node "start" and router node "decider"
    And the router "decider" declares a rule "state.x == `1`" routing to "A"
    And the router "decider" declares a default rule routing to "B"
    When the graph is compiled for execution
    Then the graph compiles with the router node "decider"
    And router rule target "A" is not the pipeline entry point

  Scenario: First matching rule wins
    Given a router node "triage" with first-match-wins rules
    And the router rule "state.severity == `9`" routes to "critical"
    And the router rule "state.severity == `3`" routes to "normal"
    And the router has a default rule routing to "fallback"
    When the router evaluates state where severity is 9
    Then the router routes the run to "critical"
    And the run does not route to "normal" or "fallback"

  Scenario: Out-of-range state falls through to the first matching rule in order
    Given a router node "triage" with first-match-wins rules
    And the router rule "state.severity == `9`" routes to "critical"
    And the router rule "state.severity == `3`" routes to "normal"
    And the router has a default rule routing to "fallback"
    When the router evaluates state where severity is 5
    Then the router routes the run to "fallback"

  Scenario: Default rule routes unmatched state
    Given a router node "decider" with first-match-wins rules
    And the router rule "state.env == 'prod'" routes to "prod-deploy"
    And the router has a default rule routing to "staging-deploy"
    When the router evaluates state where env is "development"
    Then the router routes the run to "staging-deploy"

  Scenario: No matching rule without a default is refused at routing time
    Given a router node "decider" with first-match-wins rules
    And the router rule "state.env == 'prod'" routes to "prod-deploy"
    When the router evaluates state where env is "development"
    Then routing refuses with RouterNoMatchError

  Scenario: A no-match run is terminalized as router_no_match, not failed
    Given a running pipeline whose router node matches no rule and has no default
    When the pipeline engine encounters the RouterNoMatchError
    Then the run is terminalized with the status "router_no_match"
    And the run error code is "router.no_match"
    And the run is not classified as "failed"
    And "router_no_match" is a terminal run status

  Scenario: Classifier mode routes on the LLM-selected node label
    Given a classifier router node "decider"
    And the router rule label "go_a" routes to "A"
    And the router has a default rule routing to "B"
    When the router evaluates state whose LLM-selected node is "go_a"
    Then the router routes the run to "A"

  Scenario: Classifier mode falls back to the default without an LLM decision
    Given a classifier router node "decider"
    And the router rule label "go_a" routes to "A"
    And the router has a default rule routing to "B"
    When the router evaluates state with no LLM-selected node
    Then the router routes the run to "B"

  Scenario: Compile-time enforcement refuses a new router without a default rule
    Given a pipeline graph with entry node "start" and router node "bad-router"
    And the router "bad-router" declares a rule "state.x == `1`" routing to "A" without a default
    When the graph is compiled for execution
    Then compiling refuses with RouterConfigError