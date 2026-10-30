from modulo.api.routes.system_maps import _fingerprint
from modulo.core.system_maps_discovery import DiscoveryDraft


def test_discovery_fingerprint_is_stable() -> None:
    draft = DiscoveryDraft(nodes=(), relationships=(), warnings=(), skipped=())
    assert _fingerprint(draft) == _fingerprint(draft)
