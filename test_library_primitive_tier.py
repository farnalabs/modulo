import uuid

import pytest

from modulo.db.crud.library_primitive import list_library_primitives
from modulo.db.models.library_primitive import LibraryPrimitive

from conftest import TierFilterTestBase

ORG_ID = uuid.uuid4()


class TestLibraryPrimitiveTierFilter(TierFilterTestBase):
    crud_func = list_library_primitives
    model_class = LibraryPrimitive
    org_id_required = True
    org_id = ORG_ID

    @pytest.mark.asyncio
    async def test_excluded_tiers_with_search_and_type(self):
        self.session.execute = self.mock_execute(count=1)
        result = await self.crud_func(
            self.session,
            org_id=self.org_id,
            primitive_type="schema",
            search="test",
            excluded_tiers=["preview"],
        )
        assert result.total == 1
