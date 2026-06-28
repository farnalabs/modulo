Feature: OpenTelemetry Traces
  As a platform operator
  I want pipeline runs to produce OpenTelemetry traces
  So that I can monitor and debug agentic workflows

  Scenario: OTel traces capture chain spans
    Given OpenTelemetry is configured
    And a pipeline run has completed
    When the OTel span exporter captures the trace
    Then the trace contains a span for each node execution
    And the trace contains attributes for organisation_id and pipeline_id

  Scenario: Tool execution creates child spans
    Given a pipeline run with tool invocations
    When the OTel span exporter captures the trace
    Then each tool invocation has a child span under its parent node span

  Scenario: No credentials in span attributes
    Given a pipeline run with connector operations
    When the OTel span exporter captures the trace
    Then no credential fields appear in span attributes

  Scenario: Telemetry disabled produces no spans
    Given OpenTelemetry is disabled
    When a pipeline run completes
    Then no OTel spans are exported
