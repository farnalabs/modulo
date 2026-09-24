Feature: MCP Library Browse
  As an MCP client
  I want to browse and search the library via MCP
  So that AI assistants can discover reusable primitives

  The MCP server speaks JSON-RPC over the StreamableHTTP transport at POST /mcp —
  the legacy /mcp/tools/call HTTP surface no longer exists. The real shipped
  library-browse surface is the `search_library` tool (read-only, pinned at the
  `resource.read_only` viewer floor in the centralized scope gate). These
  scenarios call the REAL `search_library` tool handler directly with the request
  ContextVars hydrated by hand (the `trigger.feature` / `review_hitl.feature`
  re-anchor pattern), network-free and DB-free, with only the auth re-validation
  and DB/list seams patched. The FastMCP invoke/dispatch layer itself is not
  exercised here.

  Background:
    Given an MCP server is running at /mcp

  Scenario: MCP lists library primitives
    Given the organisation has 3 local primitives
    When the MCP client browses the library
    Then the response contains the list of primitives
    And each primitive has id, name, and type

  Scenario: MCP searches library primitives
    Given the organisation has a primitive named "PRD Input Schema"
    When the MCP client searches the library for "PRD"
    Then the response contains the primitive named "PRD Input Schema"

  Scenario: MCP library browse is read-only
    Given the organisation has 3 local primitives
    When the MCP client browses the library
    Then the response is read-only
    And no primitives are created or modified

  Scenario: MCP caller without library browse scope is blocked
    Given the MCP caller's allowed_tools scope excludes the library
    When the MCP client tries to browse the library
    Then the response carries an error
    And the tool returns error "insufficient_scope"
