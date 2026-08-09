from typing import Any


def unsafe_object_seed(initial_state: dict[str, Any], broker: Any) -> None:
    # ruleid: non-serializable-in-langgraph-state
    initial_state["_broker"] = broker


def unsafe_attribute_seed(initial_state: dict[str, Any]) -> None:
    # ruleid: non-serializable-in-langgraph-state
    initial_state["connector"] = self.connector


def unsafe_call_seed(initial_state: dict[str, Any]) -> None:
    # ruleid: non-serializable-in-langgraph-state
    initial_state["session"] = get_registry().get_or_create(run_id)


def safe_int(initial_state: dict[str, Any]) -> None:
    # ok: non-serializable-in-langgraph-state
    initial_state["x"] = 1


def safe_float(initial_state: dict[str, Any]) -> None:
    # ok: non-serializable-in-langgraph-state
    initial_state["f"] = 1.5


def safe_str(initial_state: dict[str, Any]) -> None:
    # ok: non-serializable-in-langgraph-state
    initial_state["name"] = "modulo"


def safe_bool(initial_state: dict[str, Any]) -> None:
    # ok: non-serializable-in-langgraph-state
    initial_state["flag"] = True


def safe_none(initial_state: dict[str, Any]) -> None:
    # ok: non-serializable-in-langgraph-state
    initial_state["nothing"] = None


def safe_list(initial_state: dict[str, Any]) -> None:
    # ok: non-serializable-in-langgraph-state
    initial_state["items"] = [1, 2, 3]


def safe_dict(initial_state: dict[str, Any]) -> None:
    # ok: non-serializable-in-langgraph-state
    initial_state["config"] = {"a": 1}
