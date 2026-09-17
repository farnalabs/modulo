# Testing Strategy

## Test Suites

| Suite | Files | Runs Against | CI Trigger | Notes |
|-------|-------|-------------|------------|-------|
| Connector Conformance | `backend/tests/connectors/test_conformance.py` | filesystem, shell, rest | Every push | Parametrised per registered connector type; auto-discovers fixtures via `conftest.py` |
| REST Connector Contract | `backend/tests/connectors/test_rest_contract.py` | rest | Every push | In-process MockTransport; tests auth modes, pagination, records_path, on_unknown, allowed_hosts, write |
| Shell Connector Contract | `backend/tests/connectors/test_shell_contract.py` | shell | Every push | Real subprocess execution via SubprocessRuntimeProvider; tests stdout, exit codes, allowlist enforcement |
| npm Connector Contract | `backend/tests/connectors/test_npm_contract.py` | npm | Every push | VCR-replayed cassettes (offline); tests search, package fetch, error paths |
| PyPI Connector Contract | `backend/tests/connectors/test_pypi_contract.py` | pypi | Every push | VCR-replayed cassettes (offline); tests package fetch, error paths |
| Filesystem Connector | `backend/tests/connectors/test_filesystem_conformance.py` | filesystem | Every push | Extended filesystem-specific tests beyond conformance |

## VCR Pipeline

Cassettes live in `backend/tests/cassettes/`. CI runs with `record_mode=none` (replay only).
To record new cassettes: `VCR_RECORD_MODE=once uv run pytest tests/connectors/test_npm_contract.py -v`
