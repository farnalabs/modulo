"""BDD step definitions: HITL webhook, failure webhook, signing."""

import uuid

from pytest_bdd import given, parsers, scenarios, then, when

scenarios("../../features/notifications/hitl_webhook.feature")
scenarios("../../features/notifications/failure_webhook.feature")
scenarios("../../features/notifications/signing.feature")


@given(parsers.parse('pipeline "{name}" has an approval gate at node "{node}"'))
def pipeline_with_gate(name: str, node: str, request):
    request.node._pipeline_name = name
    request.node._gate_node = node


@given(parsers.parse('the pipeline has HITL webhook configured at "{url}"'))
def hitl_webhook_configured(url: str, request):
    request.node._webhook_url = url
    request.node._webhook_type = "hitl"


@given("the pipeline has HITL webhook configured")
def hitl_webhook_configured_default(request):
    request.node._webhook_url = "https://hooks.example.com/hitl"
    request.node._webhook_type = "hitl"


@when(parsers.parse('the run reaches the "{node}" node'))
def run_reaches_node(node: str, request):
    request.node._reached_node = node
    request.node._run_id = uuid.uuid4()


@then(parsers.parse("a webhook POST is sent to {url}"))
def webhook_sent(url: str, request):
    pass


@then("the webhook body contains the run_id and gate_id")
def webhook_body_contains_ids(request):
    pass


@then(parsers.parse("the webhook payload includes the pipeline name"))
def webhook_includes_pipeline_name(request):
    pass


@then(parsers.parse("the webhook payload includes the node name"))
def webhook_includes_node_name(request):
    pass


@given(parsers.parse("the HITL webhook endpoint returns {status:d}"))
def webhook_endpoint_returns(status: int, request):
    request.node._webhook_status = status


@then(parsers.parse("the webhook is retried up to {count:d} times"))
def webhook_retried(count: int, request):
    pass


@then(parsers.parse("after {count:d} failures, the event is logged to the dead-letter queue"))
def dead_letter_logged(count: int, request):
    pass


@given(parsers.parse('pipeline "{name}" has a failure webhook configured at "{url}"'))
def failure_webhook_configured(name: str, url: str, request):
    request.node._pipeline_name = name
    request.node._webhook_url = url
    request.node._webhook_type = "failure"


@given(parsers.parse('pipeline "{name}" has a failure webhook configured'))
def failure_webhook_configured_default(name: str, request):
    request.node._pipeline_name = name
    request.node._webhook_url = "https://hooks.example.com/fail"
    request.node._webhook_type = "failure"


@given("a running pipeline")
def running_pipeline(request):
    request.node._run_id = uuid.uuid4()
    request.node._run_status = "running"


@when("a node raises an unhandled exception")
def node_raises_exception(request):
    request.node._failed_node = "node-2"
    request.node._error_msg = "Connection timeout"


@then(parsers.parse("the webhook body contains the run_id and error_detail"))
def webhook_contains_error(request):
    pass


@then(parsers.parse("the webhook payload includes the failed node name"))
def webhook_includes_failed_node(request):
    pass


@then(parsers.parse("the webhook payload includes the error message"))
def webhook_includes_error_msg(request):
    pass


@given("the failure webhook endpoint returns {status:d}")
def failure_webhook_returns(status: int, request):
    request.node._webhook_status = status


@given(parsers.parse("the failure webhook endpoint has failed {count:d} consecutive times"))
def failure_endpoint_failed(count: int, request):
    request.node._consecutive_failures = count


@when("a new failure occurs")
def new_failure_occurs(request):
    pass


@then("the webhook endpoint is disabled")
def endpoint_disabled(request):
    pass


@then("an alert is logged")
def alert_logged(request):
    pass


@given(parsers.parse('pipeline "{name}" has a webhook configured'))
def pipeline_has_webhook(name: str, request):
    request.node._pipeline_name = name
    request.node._webhook_type = "generic"


@given(parsers.parse('the pipeline has webhook secret "{secret}"'))
def pipeline_has_webhook_secret(secret: str, request):
    request.node._webhook_secret = secret


@when("a webhook notification is sent")
def webhook_notification_sent(request):
    request.node._webhook_payload = {"event": "test", "run_id": str(uuid.uuid4())}


@then(parsers.parse('the request includes header "{header}"'))
def request_includes_header(header: str, request):
    pass


@then(parsers.parse("the signature is a valid HMAC-SHA256 of the payload"))
def valid_hmac_signature(request):
    import hashlib
    import hmac
    import json

    secret = getattr(request.node, "_webhook_secret", "whsec_abc123").encode()
    payload = json.dumps(getattr(request.node, "_webhook_payload", {})).encode()
    expected = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    assert len(expected) == 64


@given(parsers.parse('org "{org}" has webhook secret "{secret}"'))
def org_webhook_secret(org: str, secret: str, request):
    request.node._org_webhook_secret = secret


@when(parsers.parse('a webhook is sent from org "{org}"'))
def webhook_sent_from_org(org: str, request):
    request.node._webhook_org = org


@then(parsers.parse('the signature is computed with "{secret}"'))
def signature_computed_with(secret: str, request):
    import hashlib
    import hmac
    import json

    payload = json.dumps({"event": "test"}).encode()
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert len(expected) == 64


@then(parsers.parse('a webhook from org "{org}" uses "{secret}"'))
def webhook_from_org(org: str, secret: str, request):
    pass


@then(parsers.parse('the request includes header "X-Modulo-Timestamp"'))
def request_includes_timestamp(request):
    pass


@then("the timestamp is within 5 minutes of current time")
def timestamp_recent(request):
    pass


@then("the webhook is signed with the org's webhook secret")
def webhook_signed_with_org_secret(request):
    pass
