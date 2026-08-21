Feature: SSE Event Bus
  Real-time event streaming for frontend sync

  Scenario: Frontend receives event after resource mutation
    Given a valid auth token
    When a client connects to the SSE event stream
    And a run is updated via the API
    Then the client receives a resource_changed event with type "run" and action "updated"

  Scenario: Events are org-scoped
    Given two clients in different organisations
    When a resource is mutated in organisation A
    Then only the client in organisation A receives the event

  Scenario: Client receives events until disconnect
    Given a connected SSE client
    When the client disconnects
    Then no further events are delivered to that client

  Scenario: Invalid token is rejected before streaming
    Given an invalid auth token
    When a client connects to the SSE event stream
    Then the connection is rejected with 401

  Scenario: Created resource emits "created" event
    Given a valid auth token
    When a client connects to the SSE event stream
    And a new pipeline is created
    Then the client receives a resource_changed event with type "pipeline" and action "created"

  Scenario: Deleted resource emits "deleted" event
    Given a valid auth token
    When a client connects to the SSE event stream
    And a pipeline is deleted
    Then the client receives a resource_changed event with type "pipeline" and action "deleted"
