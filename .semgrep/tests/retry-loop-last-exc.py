def unsafe_retry() -> None:
    # ruleid: retry-loop-last-exc
    for _attempt in range(3):
        try:
            connect()
            return
        except ConnectionError as exc:
            _ = exc
            continue
    raise RuntimeError("unreachable")


def safe_final_reraise() -> None:
    # ok: retry-loop-last-exc
    for attempt in range(3):
        try:
            connect()
            return
        except ConnectionError as exc:
            _ = exc
            if attempt == 2:
                raise
    raise RuntimeError("unreachable")


def safe_result_fallback() -> str:
    for _attempt in range(3):
        try:
            connect()
            return "connected"
        except ConnectionError as exc:
            _ = exc
            continue
    return "unavailable"


def safe_final_reraise_with_logging_handler() -> None:
    # ok: retry-loop-last-exc
    for attempt in range(3):
        try:
            connect()
            return
        except ConnectionError:
            if attempt == 2:
                raise
        except OSError as exc:
            log(exc)
            raise
    raise RuntimeError("unreachable")
