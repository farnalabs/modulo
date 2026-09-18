"""Regression guards for the msgpack-serializability of LangGraph state.

Outage PR #895: a ``RunEventBroker`` object was seeded into LangGraph
``initial_state["_broker"]``; the first checkpoint write then failed with
``TypeError: Type is not msgpack serializable`` and every run died. The
checkpointer serializes state values via ``JsonPlusSerializer.dumps_typed``
(the same ``self.serde.dumps_typed`` path ``ModuloPostgresSaver.aput_writes``
uses). These tests pin that failure mode so the semgrep rule
``non-serializable-in-langgraph-state`` has a behavioural backing.
"""

import asyncio

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


class _Unserializable:
    pass


def test_live_object_raises_type_error_on_dumps_typed():
    """A live object (the PR #895 shape) must fail the checkpoint serde path."""
    with pytest.raises(TypeError, match="msgpack serializable"):
        JsonPlusSerializer().dumps_typed(_Unserializable())


def test_asyncio_event_raises_type_error_on_dumps_typed():
    """asyncio.Event is not msgpack-serializable either - never seed it."""
    with pytest.raises(TypeError, match="msgpack serializable"):
        JsonPlusSerializer().dumps_typed(asyncio.Event())


def test_primitives_serialize_on_dumps_typed():
    """Primitives and collections of primitives serialize cleanly."""
    type_str, blob = JsonPlusSerializer().dumps_typed({"run_id": "abc", "org_id": 1, "items": [1, 2, "three"]})
    assert type_str == "msgpack"
    assert blob
