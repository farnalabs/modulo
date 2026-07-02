import sys
from pathlib import Path

import pytest

codebase_dir = Path(__file__).resolve().parents[2]
scripts_dir = codebase_dir / "scripts"
if scripts_dir.is_dir():
    sys.path.insert(0, str(codebase_dir))


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
