import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from modulo.model_backends.stub import StubModelBackend

codebase_dir = Path(__file__).resolve().parents[2]
scripts_dir = codebase_dir / "scripts"
if scripts_dir.is_dir():
    sys.path.insert(0, str(codebase_dir))


@pytest.fixture
def stub_backend() -> StubModelBackend:
    return StubModelBackend()


@pytest.fixture
def stub_backend_factory() -> Callable[[Mapping[str, str]], StubModelBackend]:
    def _factory(fixture_map: Mapping[str, str]) -> StubModelBackend:
        return StubModelBackend(fixture_map=fixture_map)

    return _factory
