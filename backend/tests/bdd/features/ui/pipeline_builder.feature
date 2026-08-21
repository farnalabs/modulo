Feature: Pipeline Builder
  As a pipeline designer
  I want to build and modify pipeline graphs visually
  So that I can compose agentic workflows without writing code

# UI behaviour not implemented in the current frontend - deselected from runs (see test_ui.py / feature).
@awaiting-implementation
  Scenario: Load pipeline builder page
    Given I am on the pipeline builder page
    Then I see the pipeline canvas
    And there are available agents in the sidebar

# UI behaviour not implemented in the current frontend - deselected from runs (see test_ui.py / feature).
@awaiting-implementation
  Scenario: Add an agent node to the canvas
    Given I have an empty pipeline canvas
    When I drag an agent onto the canvas
    Then I see a node on the canvas

# UI behaviour not implemented in the current frontend - deselected from runs (see test_ui.py / feature).
@awaiting-implementation
  Scenario: Connect two nodes with an edge
    Given there are two nodes on the pipeline canvas
    When I connect two nodes with an edge
    Then I see an edge between the two nodes

# UI behaviour not implemented in the current frontend - deselected from runs (see test_ui.py / feature).
@awaiting-implementation
  Scenario: Configure an agent's prompt
    Given there is a node on the pipeline canvas
    When I configure the agent's prompt
    Then the agent configuration panel is shown

# UI behaviour not implemented in the current frontend - deselected from runs (see test_ui.py / feature).
@awaiting-implementation
  Scenario: Delete a node from the canvas
    Given there is a node on the pipeline canvas
    When I delete a node from the canvas
    Then the node is removed from the canvas
