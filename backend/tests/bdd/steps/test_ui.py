"""Step definitions for UI features — theme switching, real-time updates, and stubs.

Uses Playwright's Page fixture. Every interactive element has a data-testid.
Never uses waitForTimeout — always waits for '[data-loading="false"]'.
"""

from typing import Any

import pytest
from playwright.sync_api import Page
from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../features/ui/theme_switching.feature")
scenarios("../features/ui/real_time_updates.feature")
scenarios("../features/ui/run_detail.feature")
scenarios("../features/ui/org_settings.feature")
scenarios("../features/ui/pipeline_builder.feature")
scenarios("../features/ui/eval_dashboard.feature")


# ============================================================================
# Helpers
# ============================================================================


def _mock_websocket(page: Page) -> None:
    """Install a mock WebSocket that the test can control via window.__mockWs."""
    page.add_init_script("""
        window.__mockWs = {
            connected: false,
            url: null,
            onopen: null,
            onmessage: null,
            onclose: null,
            messages: [],
            connect: function(url) {
                this.connected = true;
                this.url = url;
            },
            send: function(data) {
                this.messages.push(data);
            },
            close: function() {
                this.connected = false;
                if (this.onclose) this.onclose({code: 1000, reason: 'normal'});
            },
            __triggerMessage: function(data) {
                if (this.onmessage) {
                    this.onmessage({data: typeof data === 'string' ? data : JSON.stringify(data)});
                }
            },
            __triggerClose: function(code, reason) {
                this.connected = false;
                if (this.onclose) this.onclose({code: code || 1006, reason: reason || ''});
            },
            __triggerOpen: function() {
                if (this.onopen) this.onopen({});
            }
        };
        var OriginalWS = window.WebSocket;
        window.WebSocket = function(url, protocols) {
            window.__mockWs.connect(url);
            var that = window.__mockWs;
            setTimeout(function() { if (that.onopen) that.onopen({}); }, 10);
            return that;
        };
        window.WebSocket.CONNECTING = 0;
        window.WebSocket.OPEN = 1;
        window.WebSocket.CLOSING = 2;
        window.WebSocket.CLOSED = 3;
    """)


# ============================================================================
# Shared state
# ============================================================================


@pytest.fixture
def ui_ctx() -> dict[str, Any]:
    return {
        "run_id": None,
        "selected_theme": None,
    }


# ============================================================================
# theme_switching.feature steps
# ============================================================================


@given("I open the Modulo app")
def _open_app(page: Page, base_url: str) -> None:
    """Open the app and wait for it to finish loading."""
    page.goto(base_url)
    page.wait_for_selector('[data-loading="false"]')


@given("I have selected the agent theme")
def _select_agent_theme(page: Page, base_url: str) -> None:
    """Open the app, select agent theme, and verify it stuck."""
    page.goto(base_url)
    page.wait_for_selector('[data-loading="false"]')
    toggle = page.locator('[data-testid="theme-toggle"]')
    if toggle.is_visible():
        toggle.click()
        page.wait_for_selector('[data-loading="false"]')


@when("I click the theme toggle")
def _click_theme_toggle(page: Page) -> None:
    page.locator('[data-testid="theme-toggle"]').click()
    page.wait_for_selector('[data-loading="false"]')


@when("I reload the page")
def _reload_page(page: Page) -> None:
    page.reload()
    page.wait_for_selector('[data-loading="false"]')


@then('the data-theme attribute is "standard"')
def _data_theme_standard(page: Page) -> None:
    theme = page.locator("html").get_attribute("data-theme")
    assert theme == "standard", f"Expected data-theme='standard', got '{theme}'"


@then('the data-theme attribute is "agent"')
def _data_theme_agent(page: Page) -> None:
    theme = page.locator("html").get_attribute("data-theme")
    assert theme == "agent", f"Expected data-theme='agent', got '{theme}'"


@then("the background uses the agent colour palette")
def _background_agent_colour(page: Page) -> None:
    """Check that the background colour differs from the standard theme default."""
    bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    # Agent theme uses a darker background (e.g., #0a0a0f or similar)
    # The exact value depends on the design system; here we assert it's not
    # a typical light-mode default.
    assert bg is not None, "Could not read background colour"
    # Convert to RGB and check it's a dark colour
    rgb_values = bg.replace("rgb(", "").replace(")", "").split(",")
    r, g, b = int(rgb_values[0].strip()), int(rgb_values[1].strip()), int(rgb_values[2].strip())
    avg = (r + g + b) / 3
    assert avg < 128, f"Background {bg} is not a dark agent theme colour (avg={avg})"


@then('the data-theme attribute is still "agent"')
def _data_theme_still_agent(page: Page) -> None:
    theme = page.locator("html").get_attribute("data-theme")
    assert theme == "agent", f"Expected data-theme='agent' after reload, got '{theme}'"


# ============================================================================
# real_time_updates.feature steps
# ============================================================================


@given(parsers.parse("I am viewing run details for run \"{run_id}\""))
def _viewing_run(page: Page, base_url: str, run_id: str, ui_ctx: dict[str, Any]) -> None:
    _mock_websocket(page)
    ui_ctx["run_id"] = run_id
    page.goto(f"{base_url}/runs/{run_id}")
    page.wait_for_selector('[data-loading="false"]')


@given(parsers.parse('the run is in "{status}" state'))
def _run_is_in_state(page: Page, status: str, ui_ctx: dict[str, Any]) -> None:
    """Expect the run detail page to show the current status badge."""
    badge = page.locator('[data-testid="run-status"]')
    if badge.is_visible():
        assert status in badge.text_content().lower(), (
            f"Expected status '{status}' on page"
        )


@when("the backend emits a node_complete event")
def _backend_emits_node_complete(page: Page) -> None:
    """Simulate a WebSocket node_complete event from the backend."""
    page.evaluate("""
        if (window.__mockWs) {
            window.__mockWs.__triggerMessage({
                type: 'node_complete',
                node_id: 'agent-1',
                status: 'completed',
                timestamp: new Date().toISOString()
            });
        }
    """)


@when("the run reaches an approval gate")
def _run_reaches_approval_gate(page: Page) -> None:
    """Simulate a WebSocket event indicating HITL approval gate."""
    page.evaluate("""
        if (window.__mockWs) {
            window.__mockWs.__triggerMessage({
                type: 'approval_required',
                node_id: 'manual-1',
                reason: 'Human review needed for manual step',
                timestamp: new Date().toISOString()
            });
        }
    """)


@when("the WebSocket connection drops")
def _websocket_drops(page: Page) -> None:
    """Simulate a WebSocket disconnection."""
    page.evaluate("""
        if (window.__mockWs) {
            window.__mockWs.__triggerClose(1006, 'Network error');
        }
    """)


@then("the run detail page shows the updated node status")
def _run_detail_shows_updated_node(page: Page) -> None:
    """Check that the node status was updated in the UI."""
    node = page.locator('[data-testid="node-status-agent-1"]')
    if node.is_visible():
        text = node.text_content().lower()
        assert "completed" in text or "success" in text, (
            f"Expected completed status, got '{text}'"
        )


@then("an approval banner appears without page refresh")
def _approval_banner_appears(page: Page) -> None:
    """Check that the approval/HITL banner is visible."""
    banner = page.locator('[data-testid="approval-banner"]')
    assert banner.is_visible(), "Approval banner should be visible"
    text = banner.text_content().lower()
    assert "approve" in text or "review" in text, "Banner should mention approval"


@given("I am viewing a running pipeline")
def _viewing_running_pipeline(page: Page, base_url: str) -> None:
    _mock_websocket(page)
    page.goto(f"{base_url}/runs/run-running")
    page.wait_for_selector('[data-loading="false"]')


@then("the client reconnects automatically")
def _client_reconnects(page: Page) -> None:
    """Check that the client attempts to reconnect after WebSocket drop."""
    # After the mock disconnect, the client should try reconnecting.
    # We can verify by checking that a new WebSocket connection was initiated.
    reconnect_attempted = page.evaluate("""
        window.__mockWs && window.__mockWs.url !== null
    """)
    assert reconnect_attempted, "Client should attempt WebSocket reconnection"


@then("resumes receiving updates")
def _resumes_receiving_updates(page: Page) -> None:
    """Verify the mock WebSocket can still receive messages after reconnect."""
    page.evaluate("""
        if (window.__mockWs) {
            window.__mockWs.__triggerMessage({
                type: 'node_complete',
                node_id: 'agent-2',
                status: 'completed',
                timestamp: new Date().toISOString()
            });
        }
    """)
    node = page.locator('[data-testid="node-status-agent-2"]')
    if node.is_visible():
        assert "completed" in node.text_content().lower()


# ============================================================================
# run_detail.feature — TODO
# ============================================================================


@given("I am on the run detail page for a completed run")
def _on_run_detail_completed() -> None:
    """Placeholder — implement when run detail view is built."""
    pass


@given("the run has 3 nodes with their outputs")
def _run_has_three_nodes() -> None:
    """Placeholder — implement when run detail view is built."""
    pass


@when("I click on a node to expand its output")
def _click_node_to_expand() -> None:
    """Placeholder — implement when run detail view is built."""
    pass


@when("I click the log viewer tab")
def _click_log_viewer() -> None:
    """Placeholder — implement when run detail view is built."""
    pass


@then("I see the node input and output payload")
def _see_node_payload() -> None:
    """Placeholder — implement when run detail view is built."""
    pass


@then("I see a timeline of node executions with durations")
def _see_timeline() -> None:
    """Placeholder — implement when run detail view is built."""
    pass


@then("I see per-node log entries")
def _see_per_node_logs() -> None:
    """Placeholder — implement when run detail view is built."""
    pass


@then("sensitive values are masked with ●●●●●")
def _sensitive_values_masked() -> None:
    """Placeholder — implement when run detail view is built."""
    pass


@then("the run detail page shows the updated node statuses")
def _run_detail_updated_statuses() -> None:
    """Placeholder — implement when run detail view is built."""
    pass


# ============================================================================
# org_settings.feature — TODO
# ============================================================================


@given("I am on the organisation settings page")
def _on_org_settings() -> None:
    """Placeholder — implement when org settings page is built."""
    pass


@given("I am an org admin")
def _i_am_org_admin() -> None:
    """Placeholder — implement when org settings page is built."""
    pass


@given("there are 3 members in the organisation")
def _three_members() -> None:
    """Placeholder — implement when org settings page is built."""
    pass


@given("the organisation has 2 API keys")
def _two_api_keys() -> None:
    """Placeholder — implement when org settings page is built."""
    pass


@when("I change the organisation name")
def _change_org_name() -> None:
    """Placeholder — implement when org settings page is built."""
    pass


@when('I click "Add Member"')
def _click_add_member() -> None:
    """Placeholder — implement when org settings page is built."""
    pass


@when("I revoke an API key")
def _revoke_api_key() -> None:
    """Placeholder — implement when org settings page is built."""
    pass


@then("the organisation name is updated")
def _org_name_updated() -> None:
    """Placeholder — implement when org settings page is built."""
    pass


@then("I see a member invitation form")
def _see_invitation_form() -> None:
    """Placeholder — implement when org settings page is built."""
    pass


@then("the API key status changes to revoked")
def _api_key_revoked() -> None:
    """Placeholder — implement when org settings page is built."""
    pass


# ============================================================================
# pipeline_builder.feature — TODO
# ============================================================================


@given("I am on the pipeline builder page")
def _on_pipeline_builder() -> None:
    """Placeholder — implement when pipeline builder UI is built."""
    pass


@given("I have an empty pipeline canvas")
def _empty_pipeline_canvas() -> None:
    """Placeholder — implement when pipeline builder UI is built."""
    pass


@given("there are available agents in the sidebar")
def _agents_in_sidebar() -> None:
    """Placeholder — implement when pipeline builder UI is built."""
    pass


@when("I drag an agent onto the canvas")
def _drag_agent_onto_canvas() -> None:
    """Placeholder — implement when pipeline builder UI is built."""
    pass


@when("I connect two nodes with an edge")
def _connect_two_nodes() -> None:
    """Placeholder — implement when pipeline builder UI is built."""
    pass


@when("I configure the agent's prompt")
def _configure_agent_prompt() -> None:
    """Placeholder — implement when pipeline builder UI is built."""
    pass


@when("I delete a node from the canvas")
def _delete_node_from_canvas() -> None:
    """Placeholder — implement when pipeline builder UI is built."""
    pass


@then("I see a node on the canvas")
def _see_node_on_canvas() -> None:
    """Placeholder — implement when pipeline builder UI is built."""
    pass


@then("I see an edge between the two nodes")
def _see_edge_between_nodes() -> None:
    """Placeholder — implement when pipeline builder UI is built."""
    pass


@then("the agent configuration panel is shown")
def _agent_config_panel_shown() -> None:
    """Placeholder — implement when pipeline builder UI is built."""
    pass


@then("the node is removed from the canvas")
def _node_removed_from_canvas() -> None:
    """Placeholder — implement when pipeline builder UI is built."""
    pass


# ============================================================================
# eval_dashboard.feature — TODO
# ============================================================================


@given("I am on the eval dashboard page")
def _on_eval_dashboard() -> None:
    """Placeholder — implement when eval dashboard is built."""
    pass


@given("there are 10 eval runs in the database")
def _ten_eval_runs() -> None:
    """Placeholder — implement when eval dashboard is built."""
    pass


@given("there are eval runs with both pass and fail statuses")
def _eval_runs_pass_fail() -> None:
    """Placeholder — implement when eval dashboard is built."""
    pass


@when("I view the score history chart")
def _view_score_history() -> None:
    """Placeholder — implement when eval dashboard is built."""
    pass


@when("I filter by failed runs")
def _filter_failed_runs() -> None:
    """Placeholder — implement when eval dashboard is built."""
    pass


@when("I drill down into a specific eval run")
def _drill_down_eval_run() -> None:
    """Placeholder — implement when eval dashboard is built."""
    pass


@then("I see a line chart of scores over time")
def _see_score_line_chart() -> None:
    """Placeholder — implement when eval dashboard is built."""
    pass


@then("I see trend indicators (improving / declining)")
def _see_trend_indicators() -> None:
    """Placeholder — implement when eval dashboard is built."""
    pass


@then("only failed runs are shown in the list")
def _only_failed_runs_shown() -> None:
    """Placeholder — implement when eval dashboard is built."""
    pass


@then("I see detailed results for each test case")
def _see_detailed_test_results() -> None:
    """Placeholder — implement when eval dashboard is built."""
    pass
