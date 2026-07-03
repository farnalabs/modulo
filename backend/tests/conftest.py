import sys
from pathlib import Path

import pytest

conftest_path = Path(__file__).resolve()
codebase_dir = conftest_path.parents[2]
backend_src_dir = conftest_path.parents[1] / "src"
scripts_dir = codebase_dir / "scripts"
if scripts_dir.is_dir():
    sys.path.insert(0, str(codebase_dir))
    if backend_src_dir.is_dir() and str(backend_src_dir) not in sys.path:
        sys.path.insert(0, str(backend_src_dir))


@pytest.fixture
def stub_backend():
    from modulo.model_backends.stub import StubModelBackend
    return StubModelBackend()


@pytest.fixture
def stub_backend_factory():
    from modulo.model_backends.stub import StubModelBackend

    def _factory(fixture_map):
        return StubModelBackend(fixture_map=fixture_map)
    return _factory
