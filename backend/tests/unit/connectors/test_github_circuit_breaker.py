"""Circuit breaker tests for GitHubConnector — sustained failure fail-fast.

The connector trips an open state after a configurable number of consecutive
service-level failures (5xx, exhausted rate limits, transport failures), then
fails fast with ``GitHubCircuitOpenError`` until the cooldown elapses and a
half-open probe succeeds. Client errors (4xx) never count toward the breaker.
"""

import httpx
import pytest
import respx

from modulo.connectors.base import ConnectorQuery
from modulo.connectors.github import (
    GitHubAPIError,
    GitHubCircuitOpenError,
    GitHubConnector,
    GitHubNetworkError,
)


async def _noop_sleep(_delay: float) -> None:
    return None


@pytest.fixture
def connector():
    return GitHubConnector(
        token="ghp_test_token",
        circuit_failure_threshold=2,
        circuit_cooldown_seconds=30.0,
    )


@pytest.fixture
def fast_clock(monkeypatch):
    """Freeze ``time.monotonic`` and make retry sleeps instant."""
    clock = [1000.0]
    monkeypatch.setattr("modulo.connectors.github.asyncio.sleep", _noop_sleep)
    monkeypatch.setattr("modulo.connectors.github.time.monotonic", lambda: clock[0])
    return clock


@respx.mock
async def test_circuit_opens_after_threshold_failures(connector, fast_clock):
    """Repeated 500s open the circuit; the next call fails fast."""
    route = respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(500, text="Server Error"))

    for _ in range(connector._circuit_failure_threshold):
        with pytest.raises(GitHubAPIError, match="500"):
            await connector.query(ConnectorQuery(resource="repos"))

    calls_before = route.call_count
    with pytest.raises(GitHubCircuitOpenError) as excinfo:
        await connector.query(ConnectorQuery(resource="repos"))
    assert excinfo.value.error_code == "circuit_open"
    assert excinfo.value.retry_after_seconds is not None
    assert excinfo.value.retry_after_seconds > 0
    assert route.call_count == calls_before  # no network request made


@respx.mock
async def test_circuit_open_fails_fast_without_network(connector, fast_clock):
    """While open, calls raise before touching the network."""
    route = respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(503, text="Unavailable"))

    for _ in range(connector._circuit_failure_threshold):
        with pytest.raises(GitHubAPIError, match="503"):
            await connector.query(ConnectorQuery(resource="repos"))

    calls_before = route.call_count
    for _ in range(3):
        with pytest.raises(GitHubCircuitOpenError, match="circuit is open"):
            await connector.query(ConnectorQuery(resource="repos"))
    assert route.call_count == calls_before


@respx.mock
async def test_circuit_state_exposes_observability(connector, fast_clock):
    """circuit_state() reports open state, failure count and remaining cooldown."""
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(500, text="Server Error"))

    for _ in range(connector._circuit_failure_threshold):
        with pytest.raises(GitHubAPIError):
            await connector.query(ConnectorQuery(resource="repos"))

    state = connector.circuit_state()
    assert state["open"] is True
    assert state["consecutive_failures"] == connector._circuit_failure_threshold
    assert state["failure_threshold"] == 2
    assert state["cooldown_seconds"] == 30.0
    assert 0 < state["remaining_cooldown"] <= 30.0

    state_closed = GitHubConnector(token="x").circuit_state()
    assert state_closed["open"] is False
    assert state_closed["remaining_cooldown"] == 0.0


@respx.mock
async def test_circuit_recovers_after_cooldown_probe_success(connector, fast_clock):
    """A successful half-open probe closes the circuit."""
    route = respx.get("https://api.github.com/user/repos").mock(
        side_effect=lambda *_: httpx.Response(500, text="Server Error")
    )
    for _ in range(connector._circuit_failure_threshold):
        with pytest.raises(GitHubAPIError):
            await connector.query(ConnectorQuery(resource="repos"))

    with pytest.raises(GitHubCircuitOpenError):
        await connector.query(ConnectorQuery(resource="repos"))

    fast_clock[0] += 31.0  # cooldown elapsed -> half-open probe allowed
    route.mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    result = await connector.query(ConnectorQuery(resource="repos"))
    assert len(result.records) == 1

    state = connector.circuit_state()
    assert state["open"] is False
    assert state["half_open"] is False
    assert state["consecutive_failures"] == 0


@respx.mock
async def test_circuit_half_open_probe_failure_reopens(connector, fast_clock):
    """A failing half-open probe re-opens the circuit for a fresh cooldown."""
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(500, text="Server Error"))
    for _ in range(connector._circuit_failure_threshold):
        with pytest.raises(GitHubAPIError):
            await connector.query(ConnectorQuery(resource="repos"))

    fast_clock[0] += 31.0  # cooldown elapsed -> probe allowed
    with pytest.raises(GitHubAPIError, match="500"):
        await connector.query(ConnectorQuery(resource="repos"))

    state = connector.circuit_state()
    assert state["open"] is True
    assert state["consecutive_failures"] == connector._circuit_failure_threshold + 1
    assert state["remaining_cooldown"] > 0

    with pytest.raises(GitHubCircuitOpenError):
        await connector.query(ConnectorQuery(resource="repos"))


@respx.mock
async def test_client_errors_do_not_trip_circuit(connector, fast_clock):
    """4xx client errors never count toward the breaker."""
    route = respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(404, text="Not Found"))
    for _ in range(10):
        with pytest.raises(GitHubAPIError, match="404"):
            await connector.query(ConnectorQuery(resource="repos"))

    assert connector.circuit_state()["open"] is False
    assert connector.circuit_state()["consecutive_failures"] == 0
    assert route.call_count == 10


@respx.mock
async def test_success_resets_consecutive_failures(connector, fast_clock):
    """One service failure then a success resets the failure counter."""
    respx.get("https://api.github.com/user/repos").mock(
        side_effect=[httpx.Response(500, text="Server Error"), httpx.Response(200, json=[{"id": 1}])]
    )
    with pytest.raises(GitHubAPIError, match="500"):
        await connector.query(ConnectorQuery(resource="repos"))
    assert connector.circuit_state()["consecutive_failures"] == 1

    result = await connector.query(ConnectorQuery(resource="repos"))
    assert len(result.records) == 1
    assert connector.circuit_state()["open"] is False
    assert connector.circuit_state()["consecutive_failures"] == 0


@respx.mock
async def test_retry_exhaustion_counts_as_single_failure(connector, fast_clock):
    """One call that exhausts its internal retries counts as one failure."""
    route = respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(503, text="Unavailable"))
    with pytest.raises(GitHubAPIError, match="503"):
        await connector.query(ConnectorQuery(resource="repos"))
    assert route.call_count == 4  # original + 3 retries
    assert connector.circuit_state()["consecutive_failures"] == 1
    assert connector.circuit_state()["open"] is False


@respx.mock
async def test_health_check_bypasses_circuit_and_closes_it(connector, fast_clock):
    """Health checks bypass the breaker; a healthy probe closes an open circuit."""
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(500, text="Server Error"))
    for _ in range(connector._circuit_failure_threshold):
        with pytest.raises(GitHubAPIError):
            await connector.query(ConnectorQuery(resource="repos"))
    assert connector.circuit_state()["open"] is True

    respx.get("https://api.github.com/user").mock(
        return_value=httpx.Response(200, json={"login": "octocat"}, headers={"X-OAuth-Scopes": "repo, read:org"})
    )
    result = await connector.health_check()
    assert result.ok is True
    assert connector.circuit_state()["open"] is False


@respx.mock
async def test_health_check_failure_reopens_circuit(connector, fast_clock):
    """A failing health probe during an open circuit re-opens it."""
    respx.get("https://api.github.com/user/repos").mock(return_value=httpx.Response(500, text="Server Error"))
    for _ in range(connector._circuit_failure_threshold):
        with pytest.raises(GitHubAPIError):
            await connector.query(ConnectorQuery(resource="repos"))
    assert connector.circuit_state()["open"] is True

    respx.get("https://api.github.com/user").mock(return_value=httpx.Response(500, text="Server Error"))
    result = await connector.health_check()
    assert result.ok is False
    assert connector.circuit_state()["open"] is True


def test_circuit_half_open_allows_single_probe(connector, fast_clock):
    """Only one probe is admitted once the cooldown elapses."""
    connector._circuit_open = True
    connector._circuit_open_until = 1000.0  # cooldown already elapsed

    connector._check_circuit()  # admits the probe
    assert connector._circuit_half_open is True
    with pytest.raises(GitHubCircuitOpenError, match="half-open"):
        connector._check_circuit()  # second call is still blocked


def test_circuit_constructor_validation():
    with pytest.raises(ValueError, match="threshold"):
        GitHubConnector(token="x", circuit_failure_threshold=0)
    with pytest.raises(ValueError, match="cooldown"):
        GitHubConnector(token="x", circuit_cooldown_seconds=0)
    with pytest.raises(ValueError, match="cooldown"):
        GitHubConnector(token="x", circuit_cooldown_seconds=-5.0)


@respx.mock
async def test_transport_failures_trip_circuit(connector, fast_clock):
    """Connection errors count toward the breaker."""
    route = respx.get("https://api.github.com/user/repos").mock(side_effect=httpx.ConnectError("Connection refused"))
    for _ in range(connector._circuit_failure_threshold):
        with pytest.raises(GitHubNetworkError, match="connection error"):
            await connector.query(ConnectorQuery(resource="repos"))
    calls_before = route.call_count
    with pytest.raises(GitHubCircuitOpenError):
        await connector.query(ConnectorQuery(resource="repos"))
    assert route.call_count == calls_before  # open circuit never touches the network
