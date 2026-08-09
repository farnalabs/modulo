def bad_exception_before_sqlalchemy() -> None:
    try:
        run_query()
    # ruleid: exception-mro-ordering
    except SQLAlchemyError:
        pass
    except ProgrammingError:
        pass


def bad_exception_before_integrity() -> None:
    try:
        run_query()
    # ruleid: exception-mro-ordering
    except Exception:
        pass
    except IntegrityError:
        pass


def bad_dbapi_before_operational() -> None:
    try:
        run_query()
    # ruleid: exception-mro-ordering
    except DBAPIError:
        pass
    except OperationalError:
        pass


def good_programming_before_sqlalchemy() -> None:
    # ok: exception-mro-ordering
    try:
        run_query()
    except ProgrammingError:
        pass
    except SQLAlchemyError:
        pass


def good_specific_before_exception() -> None:
    # ok: exception-mro-ordering
    try:
        run_query()
    except IntegrityError:
        pass
    except Exception:
        pass


def good_only_base() -> None:
    # ok: exception-mro-ordering
    try:
        run_query()
    except Exception:
        pass


def good_only_specific() -> None:
    # ok: exception-mro-ordering
    try:
        run_query()
    except ProgrammingError:
        pass
