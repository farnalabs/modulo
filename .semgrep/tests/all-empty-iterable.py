def unsafe_dynamic(values: list[int]) -> bool:
    # ruleid: all-empty-iterable
    return all(value > 0 for value in values)


def safe_guarded(values: list[int]) -> bool:
    if not values:
        return False
    # ok: all-empty-iterable
    return all(value > 0 for value in values)


def safe_nonempty_tuple() -> bool:
    # ok: all-empty-iterable
    return all(value > 0 for value in (1, 2))


def safe_nonempty_list() -> bool:
    # ok: all-empty-iterable
    return all([value > 0 for value in [1, 2]])


def unsafe_empty_literal() -> bool:
    # ruleid: all-empty-iterable
    return all(value > 0 for value in [])
