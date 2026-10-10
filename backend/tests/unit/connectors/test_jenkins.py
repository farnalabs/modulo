"""Unit tests for the Jenkins connector using respx mock transports."""

import httpx
import pytest
import respx

from modulo.connectors.base import CIRunStatus, ConnectorPayload, ConnectorQuery
from modulo.connectors.jenkins import JenkinsConnector, _JenkinsTestDouble

_JENKINS_BASE = "http://jenkins.example.com"


@pytest.fixture
def jenkins():
    return JenkinsConnector(username="admin", token="secret", base_url=_JENKINS_BASE)


@pytest.fixture
def jenkins_double():
    return _JenkinsTestDouble()


def test_connector_type(jenkins):
    assert jenkins.connector_type.value == "jenkins"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@respx.mock
@respx.mock
@respx.mock
# ---------------------------------------------------------------------------
# trigger_run
# ---------------------------------------------------------------------------


@respx.mock
async def test_trigger_run(jenkins):
    route = respx.post(f"{_JENKINS_BASE}/job/my-job/build").mock(
        return_value=httpx.Response(201, headers={"Location": "http://jenkins.example.com/job/my-job/42/"})
    )
    run = await jenkins.trigger_run(pipeline_id="my-job")
    assert run.pipeline_id == "my-job"
    assert run.status == CIRunStatus.QUEUED
    assert route.called


@respx.mock
async def test_trigger_run_with_parameters(jenkins):
    route = respx.post(f"{_JENKINS_BASE}/job/my-job/buildWithParameters").mock(
        return_value=httpx.Response(201, headers={"Location": "http://jenkins.example.com/job/my-job/43/"})
    )
    run = await jenkins.trigger_run(pipeline_id="my-job", variables={"BRANCH": "main", "TAG": "v1"})
    assert run.pipeline_id == "my-job"
    assert run.status == CIRunStatus.QUEUED
    assert route.called


# ---------------------------------------------------------------------------
# trigger_run — run-id round-trip (queue form and build form)
# ---------------------------------------------------------------------------


@respx.mock
async def test_trigger_run_queue_location_round_trips_to_terminal_status(jenkins):
    """A queue Location must produce an id get_run_status can consume.

    Jenkins answers a build POST with ``Location: .../queue/item/<qid>`` — a
    QUEUE id, not a build number. Feeding it straight back must resolve through
    the queue item to the dispatched build, not die on a wrong-URL 404.
    """
    respx.post(f"{_JENKINS_BASE}/job/my-job/build").mock(
        return_value=httpx.Response(201, headers={"Location": "http://jenkins.example.com/queue/item/7"}),
    )
    respx.get(f"{_JENKINS_BASE}/queue/item/7/api/json").mock(
        return_value=httpx.Response(
            200,
            json={"id": 7, "executable": {"number": 42, "url": "http://jenkins.example.com/job/my-job/42/"}},
        ),
    )
    respx.get(f"{_JENKINS_BASE}/job/my-job/42/api/json").mock(
        return_value=httpx.Response(
            200,
            json={"id": "42", "number": 42, "result": "SUCCESS", "url": "http://jenkins.example.com/job/my-job/42/"},
        ),
    )
    run = await jenkins.trigger_run(pipeline_id="my-job")
    assert run.id == "my-job/queue/7"

    status = await jenkins.get_run_status(run.id)
    assert status.status == CIRunStatus.SUCCESS
    # the queue form escalates to the durable build form, itself consumable
    assert status.id == "my-job/42"
    again = await jenkins.get_run_status(status.id)
    assert again.status == CIRunStatus.SUCCESS


@respx.mock
async def test_trigger_run_queue_unresolved_stays_queued_and_reconsumable(jenkins):
    """While the queue item has no executable yet the status stays QUEUED and
    keeps the queue-form id — so the await loop can poll it again."""
    respx.post(f"{_JENKINS_BASE}/job/my-job/build").mock(
        return_value=httpx.Response(201, headers={"Location": "http://jenkins.example.com/queue/item/9"}),
    )
    respx.get(f"{_JENKINS_BASE}/queue/item/9/api/json").mock(
        return_value=httpx.Response(200, json={"id": 9, "why": "Waiting for next available executor"}),
    )
    run = await jenkins.trigger_run(pipeline_id="my-job")
    assert run.id == "my-job/queue/9"

    status = await jenkins.get_run_status(run.id)
    assert status.status == CIRunStatus.QUEUED
    assert status.id == "my-job/queue/9"
    status_again = await jenkins.get_run_status(status.id)
    assert status_again.status == CIRunStatus.QUEUED


@respx.mock
async def test_get_run_logs_queue_form_resolves_to_build(jenkins):
    respx.post(f"{_JENKINS_BASE}/job/my-job/build").mock(
        return_value=httpx.Response(201, headers={"Location": "http://jenkins.example.com/queue/item/7"}),
    )
    respx.get(f"{_JENKINS_BASE}/queue/item/7/api/json").mock(
        return_value=httpx.Response(
            200,
            json={"id": 7, "executable": {"number": 42, "url": "http://jenkins.example.com/job/my-job/42/"}},
        ),
    )
    respx.get(f"{_JENKINS_BASE}/job/my-job/42/consoleText").mock(
        return_value=httpx.Response(200, text="line1\nline2\n"),
    )
    run = await jenkins.trigger_run(pipeline_id="my-job")
    logs = await jenkins.get_run_logs(run.id)
    assert logs.lines == ["line1", "line2"]


@respx.mock
async def test_get_run_logs_queue_form_not_started_fails_loud(jenkins):
    """Logs for a build that has not left the queue must fail loud, never
    return an empty list that reads as 'no output'."""
    respx.get(f"{_JENKINS_BASE}/queue/item/7/api/json").mock(
        return_value=httpx.Response(200, json={"id": 7, "why": "Waiting for executor"}),
    )
    with pytest.raises(ValueError, match="still queued"):
        await jenkins.get_run_logs("my-job/queue/7")


async def test_get_run_status_invalid_id_fails_loud():
    """A bare/non-qualifiable id is rejected before any HTTP call — never
    reinterpreted as ``job=<bare id>``."""
    connector = JenkinsConnector(username="admin", token="secret", base_url=_JENKINS_BASE)
    with pytest.raises(ValueError, match="Invalid run_id format"):
        await connector.get_run_status("bare42")


# ---------------------------------------------------------------------------
# FAR-1141 (security): run-id job_name path traversal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "run_id",
    [
        # `..` escapes /job/ once httpx normalises the path — the credentialed
        # request lands on a completely different Jenkins endpoint.
        "job/../../computer/api/json/1",
        "my-job/../../admin/1",
        # a '.' segment silently rewrites the path too.
        "my-job/./config/1",
        # double slash / trailing slash produce empty segments.
        "my-job//42",
        # off-allowlist characters: percent-escape traversal, query/fragment
        # separators, backslash, and scheme-shaped ':'.
        "%2e%2e/secret/1",
        "my-job%2f../x/1",
        "my-job?x=/1",
        "my-job#frag/1",
        "my-job\\..\\x/1",
        "http://evil.example.com/1",
    ],
    ids=[
        "dotdot-escape",
        "dotdot-mid-path",
        "single-dot-segment",
        "empty-segment",
        "percent-encoded-traversal",
        "percent-encoded-slash",
        "query-separator",
        "fragment-separator",
        "backslash-separator",
        "scheme-shaped",
    ],
)
def test_split_build_run_id_rejects_unsafe_job_names(run_id: str):
    """Unsafe job-name segments/characters are refused at parse time."""
    from modulo.connectors.jenkins import _split_build_run_id

    with pytest.raises(ValueError, match=r"Unsafe job name|Invalid run_id format"):
        _split_build_run_id(run_id)


@pytest.mark.parametrize(
    ("run_id", "job_name", "build_number"),
    [
        ("my-job/42", "my-job", "42"),
        # folder-nested jobs are a legitimate Jenkins path shape.
        ("folder/job/my-job/42", "folder/job/my-job", "42"),
        ("my job/7", "my job", "7"),
        ("my-job-v2/12", "my-job-v2", "12"),
    ],
)
def test_split_build_run_id_accepts_safe_job_names(run_id: str, job_name: str, build_number: str):
    from modulo.connectors.jenkins import _split_build_run_id

    assert _split_build_run_id(run_id) == (job_name, build_number)


async def test_traversal_run_id_is_rejected_before_any_http_call():
    """The rejection must fire at parse time: no request is issued, so the
    traversal can never reach the Jenkins host with credentials attached."""
    connector = JenkinsConnector(username="admin", token="secret", base_url=_JENKINS_BASE)
    # no respx mock: an HTTP attempt would raise AllMockedAssertionError
    with pytest.raises(ValueError, match="Unsafe job name"):
        await connector.get_run_status("job/../../computer/api/json/1")


@respx.mock
async def test_a_nested_folder_job_name_still_reaches_the_right_path(jenkins):
    """The guard must not reject the legitimate folder-nested job shape."""
    respx.get(f"{_JENKINS_BASE}/job/folder/job/my-job/42/api/json").mock(
        return_value=httpx.Response(
            200,
            json={"id": "42", "number": 42, "result": "SUCCESS", "url": "", "timestamp": 1700000000000},
        ),
    )
    run = await jenkins.get_run_status("folder/job/my-job/42")
    assert run.status is CIRunStatus.SUCCESS


@respx.mock
async def test_trigger_run_unusable_location_fails_loud(jenkins):
    """A build response whose Location resolves to no id fails loud — never an
    empty id the next get_run_status call rejects."""
    respx.post(f"{_JENKINS_BASE}/job/my-job/build").mock(return_value=httpx.Response(201))
    with pytest.raises(ValueError, match="unrecognised Location"):
        await jenkins.trigger_run(pipeline_id="my-job")


# ---------------------------------------------------------------------------
# get_run_status
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_run_status_success(jenkins):
    respx.get(f"{_JENKINS_BASE}/job/my-job/42/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42",
                "result": "SUCCESS",
                "url": "http://jenkins.example.com/job/my-job/42/",
                "timestamp": 1700000000000,
                "duration": 120000,
            },
        )
    )
    run = await jenkins.get_run_status("my-job/42")
    assert run.status == CIRunStatus.SUCCESS
    assert run.id == "my-job/42"


@respx.mock
async def test_get_run_status_failure(jenkins):
    respx.get(f"{_JENKINS_BASE}/job/my-job/42/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42",
                "result": "FAILURE",
                "url": "http://jenkins.example.com/job/my-job/42/",
            },
        )
    )
    run = await jenkins.get_run_status("my-job/42")
    assert run.status == CIRunStatus.FAILURE


def test_parse_build_corrupt_duration(jenkins):
    run = jenkins._parse_build({"id": "42", "duration": "not-a-number"})
    assert run.duration_seconds is None


def test_parse_build_zero_duration(jenkins):
    run = jenkins._parse_build({"id": "42", "duration": 0})
    assert run.duration_seconds is None


def test_parse_build_null_id_and_timestamp_map_to_empty_strings(jenkins):
    run = jenkins._parse_build({"id": None, "timestamp": None})
    assert not run.id
    assert not run.created_at
    assert "None" not in run.id
    assert "None" not in run.created_at


@respx.mock
async def test_get_run_status_running(jenkins):
    respx.get(f"{_JENKINS_BASE}/job/my-job/42/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "42",
                "result": None,
                "url": "http://jenkins.example.com/job/my-job/42/",
            },
        )
    )
    run = await jenkins.get_run_status("my-job/42")
    assert run.status == CIRunStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# get_run_logs
# ---------------------------------------------------------------------------


@respx.mock
async def test_get_run_logs(jenkins):
    respx.get(f"{_JENKINS_BASE}/job/my-job/42/consoleText").mock(
        return_value=httpx.Response(200, text="line1\nline2\nline3\n")
    )
    logs = await jenkins.get_run_logs("my-job/42")
    assert len(logs.lines) == 3
    assert logs.lines == ["line1", "line2", "line3"]


@respx.mock
async def test_get_run_logs_with_cursor(jenkins):
    respx.get(f"{_JENKINS_BASE}/job/my-job/42/consoleText").mock(
        return_value=httpx.Response(200, text="line1\nline2\nline3\n")
    )
    logs = await jenkins.get_run_logs("my-job/42", cursor="2")
    assert logs.lines == ["line3"]


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_runs(jenkins):
    respx.get(f"{_JENKINS_BASE}/job/my-job/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "builds": [
                    {
                        "number": 1,
                        "result": "SUCCESS",
                        "timestamp": 1700000000000,
                        "duration": 60000,
                        "url": "http://jenkins.example.com/job/my-job/1/",
                    },
                    {
                        "number": 2,
                        "result": "FAILURE",
                        "timestamp": 1700000100000,
                        "duration": 30000,
                        "url": "http://jenkins.example.com/job/my-job/2/",
                    },
                ]
            },
        )
    )
    runs = await jenkins.list_runs(pipeline_id="my-job")
    assert len(runs) == 2
    assert runs[0].status == CIRunStatus.SUCCESS
    assert runs[1].status == CIRunStatus.FAILURE


# ---------------------------------------------------------------------------
# query — generic resources
# ---------------------------------------------------------------------------


@respx.mock
async def test_query_jobs(jenkins):
    respx.get(f"{_JENKINS_BASE}/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {"name": "my-job", "url": "http://jenkins.example.com/job/my-job/", "color": "blue"},
                    {"name": "other-job", "url": "http://jenkins.example.com/job/other-job/", "color": "red"},
                ]
            },
        )
    )
    q = ConnectorQuery(resource="jobs")
    result = await jenkins.query(q)
    assert len(result.records) == 2
    assert result.records[0]["name"] == "my-job"


@respx.mock
async def test_query_builds(jenkins):
    respx.get(f"{_JENKINS_BASE}/job/my-job/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "builds": [
                    {"number": 1, "result": "SUCCESS", "timestamp": 1700000000000, "duration": 60000, "url": ""},
                ]
            },
        )
    )
    q = ConnectorQuery(resource="builds", filters={"job_name": "my-job"})
    result = await jenkins.query(q)
    assert len(result.records) == 1
    assert result.records[0]["number"] == 1


@respx.mock
async def test_query_nodes(jenkins):
    respx.get(f"{_JENKINS_BASE}/computer/api/json").mock(
        return_value=httpx.Response(
            200,
            json={
                "computer": [
                    {"displayName": "master", "offline": False},
                    {"displayName": "agent-1", "offline": True},
                ]
            },
        )
    )
    q = ConnectorQuery(resource="nodes")
    result = await jenkins.query(q)
    assert len(result.records) == 2
    assert result.records[0]["displayName"] == "master"


@respx.mock
async def test_query_unsupported_resource(jenkins):
    q = ConnectorQuery(resource="invalid", filters={})
    with pytest.raises(ValueError, match="Unsupported query resource"):
        await jenkins.query(q)


@respx.mock
async def test_list_runs_non_list_builds_no_crash(jenkins):
    """A corrupt body placing a non-list in ``builds`` must fall back to an empty run list."""
    respx.get(f"{_JENKINS_BASE}/job/my-job/api/json").mock(return_value=httpx.Response(200, json={"builds": "corrupt"}))
    runs = await jenkins.list_runs(pipeline_id="my-job")
    assert runs == []


@respx.mock
async def test_list_runs_non_dict_body_no_crash(jenkins):
    """A corrupt/hostile non-dict body must degrade to an empty run list."""
    respx.get(f"{_JENKINS_BASE}/job/my-job/api/json").mock(return_value=httpx.Response(200, json=["not-a-dict"]))
    runs = await jenkins.list_runs(pipeline_id="my-job")
    assert runs == []


@respx.mock
async def test_query_jobs_non_list_jobs_no_crash(jenkins):
    """A corrupt non-list ``jobs`` page field must degrade gracefully."""
    respx.get(f"{_JENKINS_BASE}/api/json").mock(return_value=httpx.Response(200, json={"jobs": {"name": "x"}}))
    result = await jenkins.query(ConnectorQuery(resource="jobs"))
    assert not result.records
    assert result.total == 0


# ---------------------------------------------------------------------------
# write — generic resources
# ---------------------------------------------------------------------------


@respx.mock
async def test_write_build(jenkins):
    route = respx.post(f"{_JENKINS_BASE}/job/my-job/build").mock(
        return_value=httpx.Response(201, headers={"Location": "http://jenkins.example.com/job/my-job/42/"})
    )
    payload = ConnectorPayload(resource="build", data={"job_name": "my-job"})
    result = await jenkins.write(payload)
    assert result["location"] == "http://jenkins.example.com/job/my-job/42/"
    assert result["job_name"] == "my-job"
    assert route.called


@respx.mock
async def test_write_unsupported_resource(jenkins):
    payload = ConnectorPayload(resource="invalid", data={})
    with pytest.raises(ValueError, match="Unsupported write resource"):
        await jenkins.write(payload)


# ---------------------------------------------------------------------------
# Missing required params — now fail CLOSED at the job-name guard (FAR-1141)
#
# These used to build a malformed `/job//…` URL and surface whatever the host
# answered (404). An empty name is not a valid job path, so it is rejected
# before any request exists. The 404 routes stay registered on purpose: a
# regression that still issues the request raises httpx.HTTPError, not
# ValueError, and fails the assertion.
# ---------------------------------------------------------------------------


@respx.mock
async def test_trigger_run_missing_pipeline_id(jenkins):
    respx.post(f"{_JENKINS_BASE}/job//build").mock(return_value=httpx.Response(404, text="Not found"))
    with pytest.raises(ValueError, match="Unsafe job name"):
        await jenkins.trigger_run(pipeline_id="")
    assert not respx.calls


@respx.mock
async def test_list_runs_missing_pipeline_id(jenkins):
    respx.get(f"{_JENKINS_BASE}/job//api/json").mock(return_value=httpx.Response(404, text="Not found"))
    with pytest.raises(ValueError, match="Unsafe job name"):
        await jenkins.list_runs(pipeline_id="")
    assert not respx.calls


@respx.mock
async def test_query_builds_missing_job_name(jenkins):
    q = ConnectorQuery(resource="builds", filters={})
    respx.get(f"{_JENKINS_BASE}/job//api/json").mock(return_value=httpx.Response(404, text="Not found"))
    with pytest.raises(ValueError, match="Unsafe job name"):
        await jenkins.query(q)
    assert not respx.calls


@respx.mock
async def test_write_missing_job_name(jenkins):
    """A build payload with no job_name fails closed too — it is the same
    `/job/{job_name}/build` URL shape as `trigger_run`."""
    route = respx.post(f"{_JENKINS_BASE}/job//build").mock(return_value=httpx.Response(404, text="Not found"))
    with pytest.raises(ValueError, match="Unsafe job name"):
        await jenkins.write(ConnectorPayload(resource="build", data={}))
    assert not route.called


# ---------------------------------------------------------------------------
# FAR-1141 (security) sweep: every `/job/{job_name}/…` builder rejects a
# traversal value WITHOUT issuing an HTTP call
# ---------------------------------------------------------------------------

_TRAVERSAL_JOB = "../../computer/api/json"


async def _sweep_trigger_run(connector: JenkinsConnector):
    return await connector.trigger_run(pipeline_id=_TRAVERSAL_JOB)


async def _sweep_list_runs(connector: JenkinsConnector):
    return await connector.list_runs(pipeline_id=_TRAVERSAL_JOB)


async def _sweep_query_builds(connector: JenkinsConnector):
    return await connector.query(ConnectorQuery(resource="builds", filters={"job_name": _TRAVERSAL_JOB}))


async def _sweep_write_build(connector: JenkinsConnector):
    return await connector.write(ConnectorPayload(resource="build", data={"job_name": _TRAVERSAL_JOB}))


async def _sweep_get_run_status_queue_form(connector: JenkinsConnector):
    # The queue branch extracts its own job_name without _split_build_run_id.
    return await connector.get_run_status(f"{_TRAVERSAL_JOB}/queue/7")


async def _sweep_get_run_logs_queue_form(connector: JenkinsConnector):
    return await connector.get_run_logs(f"{_TRAVERSAL_JOB}/queue/7")


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(_sweep_trigger_run, id="trigger_run"),
        pytest.param(_sweep_list_runs, id="list_runs"),
        pytest.param(_sweep_query_builds, id="query-builds"),
        pytest.param(_sweep_write_build, id="write-build"),
        pytest.param(_sweep_get_run_status_queue_form, id="get_run_status-queue-form"),
        pytest.param(_sweep_get_run_logs_queue_form, id="get_run_logs-queue-form"),
    ],
)
@respx.mock
async def test_swept_site_rejects_traversal_before_any_http_call(call):
    """Each swept site must fail at the guard, never on the wire.

    `@respx.mock` is active with NO routes registered: an attempted request
    raises `AllMockedAssertionError`, which does not match `Unsafe job name`,
    so this test fails if a single site skips the guard. `respx.calls` empty
    is the second, independent witness that no request was issued.
    """
    connector = JenkinsConnector(username="admin", token="secret", base_url=_JENKINS_BASE)
    with pytest.raises(ValueError, match="Unsafe job name"):
        await call(connector)
    assert not respx.calls


# ---------------------------------------------------------------------------
# Corrupt list payload hardening
# ---------------------------------------------------------------------------


@respx.mock
async def test_list_runs_corrupt_body_no_crash(jenkins):
    """A non-dict body from the builds endpoint must degrade to an empty run
    list instead of crashing with AttributeError on ``.get()``."""
    respx.get(f"{_JENKINS_BASE}/job/my-job/api/json").mock(return_value=httpx.Response(200, json=["garbage"]))
    runs = await jenkins.list_runs(pipeline_id="my-job")
    assert not runs


@respx.mock
async def test_list_runs_non_list_builds_value_no_crash(jenkins):
    """A corrupt body placing a non-list in ``builds`` must fall back to an
    empty run list instead of iterating a bare string."""
    respx.get(f"{_JENKINS_BASE}/job/my-job/api/json").mock(return_value=httpx.Response(200, json={"builds": "boom"}))
    runs = await jenkins.list_runs(pipeline_id="my-job")
    assert not runs


@respx.mock
async def test_query_jobs_corrupt_body_no_crash(jenkins):
    """A non-dict body from the jobs endpoint must degrade to an empty page."""
    respx.get(f"{_JENKINS_BASE}/api/json").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await jenkins.query(ConnectorQuery(resource="jobs"))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_builds_corrupt_body_no_crash(jenkins):
    """A non-dict body from the builds endpoint must degrade to an empty page."""
    respx.get(f"{_JENKINS_BASE}/job/my-job/api/json").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await jenkins.query(ConnectorQuery(resource="builds", filters={"job_name": "my-job"}))
    assert not result.records
    assert result.total == 0


@respx.mock
async def test_query_nodes_corrupt_body_no_crash(jenkins):
    """A non-dict body from the nodes endpoint must degrade to an empty page."""
    respx.get(f"{_JENKINS_BASE}/computer/api/json").mock(return_value=httpx.Response(200, json=["garbage"]))
    result = await jenkins.query(ConnectorQuery(resource="nodes"))
    assert not result.records
    assert result.total == 0


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


async def test_double_trigger_run(jenkins_double):
    run = await jenkins_double.trigger_run(pipeline_id="my-job")
    assert run.status == CIRunStatus.QUEUED
    assert len(jenkins_double._builds) == 1


async def test_double_get_run_status(jenkins_double):
    run = await jenkins_double.get_run_status("my-job/42")
    assert run.status == CIRunStatus.SUCCESS


async def test_double_get_run_logs(jenkins_double):
    logs = await jenkins_double.get_run_logs("my-job/42")
    assert logs.lines == ["line1", "line2"]


async def test_double_list_runs(jenkins_double):
    runs = await jenkins_double.list_runs(pipeline_id="my-job")
    assert len(runs) == 1
    assert runs[0].status == CIRunStatus.SUCCESS


# ---------------------------------------------------------------------------
# FAR-1141 security sweep: remaining fail-loud branches
# ---------------------------------------------------------------------------


def test_reject_unsafe_job_name_rejects_a_non_string():
    """A dict/list/None job name (Any-typed filter/payload) is refused — never
    f-stringed into a credentialed path."""
    from modulo.connectors.jenkins import _reject_unsafe_job_name

    with pytest.raises(ValueError, match="is not a string job path"):
        _reject_unsafe_job_name(None, "run id")  # type: ignore[arg-type]


async def test_queue_executable_404_fails_loud(jenkins):
    """An evicted queue item (404) fails loud instead of polling a dead id."""
    from unittest.mock import AsyncMock

    client = AsyncMock()
    client.get.return_value = httpx.Response(
        404, request=httpx.Request("GET", f"{_JENKINS_BASE}/queue/item/7/api/json")
    )
    with pytest.raises(ValueError, match="no longer exists"):
        await jenkins._queue_executable(client, "7", "my-job/queue/7")


async def test_queue_executable_non_object_body_fails_loud(jenkins):
    """A non-object JSON body cannot resolve a build — fail loud, never guess."""
    from unittest.mock import AsyncMock

    client = AsyncMock()
    client.get.return_value = httpx.Response(
        200,
        json="not-an-object",
        request=httpx.Request("GET", f"{_JENKINS_BASE}/queue/item/7/api/json"),
    )
    with pytest.raises(ValueError, match="non-object body"):
        await jenkins._queue_executable(client, "7", "my-job/queue/7")
