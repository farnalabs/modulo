import uuid

from modulo.db.crud.model_backend import list_model_backends
from modulo.db.models.model_backend import ModelBackend

from .conftest import TierFilterTestBase

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class TestModelBackendTierFilter(TierFilterTestBase):
    crud_func = list_model_backends
    model_class = ModelBackend
    org_id_required = True
    org_id = _ORG_ID
    empty_count = 2
    preview_count = 1
