"""Integration test for the FAR-1735 narrow ``jsonb_set`` trigger rotation write.

Runs ``_rotate_triggers`` against a real Postgres (Testcontainers, real
migrations) on a row carrying NESTED non-secret config keys, proving the
narrowed ``jsonb_set`` write rotates only the secret key and leaves every
non-secret key — including nested JSON — byte-identical in the database.
The unit-tier tests assert the SQL text; this test executes it.
"""

import json
import uuid

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.core.fernet_rotation import _rotate_triggers
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration

_EVENT_FILTERS = {
    "accepted_events": ["push", "pull_request"],
    "value_filters": [{"path": "repository.name", "op": "eq", "value": "modulo"}],
}


@pytest_asyncio.fixture(scope="module")
async def rotation_trigger(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_user: uuid.UUID,
    old_key: str,
) -> uuid.UUID:
    """A webhook trigger whose hmac_secret is ciphertext under *old_key* and
    whose config carries nested non-secret keys that rotation must preserve."""
    trigger_id = uuid.uuid4()
    old_ct = Fernet(old_key.encode()).encrypt(b"rotate-me-hmac").decode()
    config = {
        "hmac_secret": old_ct,
        "url": "https://example.com/hook",
        "event_filters": _EVENT_FILTERS,
        "work_item_ref_paths": [".agents"],
    }
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO triggers (id, organisation_id, pipeline_id, "
                "trigger_type, active, max_concurrent_runs, config_json, account_id) "
                "VALUES (:id, :oid, :pid, 'webhook', true, 5, (:config)::json, :uid)"
            ),
            {
                "id": str(trigger_id),
                "oid": str(test_org),
                "pid": str(test_pipeline),
                "config": json.dumps(config),
                "uid": str(test_user),
            },
        )
    return trigger_id


@pytest.fixture(scope="module")
def old_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture
def new_key() -> str:
    return Fernet.generate_key().decode()


async def test_narrow_jsonb_set_write_preserves_nested_non_secret_keys(
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    rotation_trigger: uuid.UUID,
    old_key: str,
    new_key: str,
) -> None:
    """Rotation's UPDATE narrows to the secret key via ``jsonb_set``: the
    secret is re-encrypted under the NEW key while the url, the nested
    ``event_filters`` document and ``work_item_ref_paths`` survive the real
    write unchanged."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_rls_org(session, test_org)
        count = await _rotate_triggers(
            session,
            Fernet(new_key.encode()),
            Fernet(old_key.encode()),
            new_key=new_key,
            old_key=old_key,
        )

    assert count == 1

    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT config_json FROM triggers WHERE id = :id"),
                {"id": str(rotation_trigger)},
            )
        ).one()
    config = row.config_json if isinstance(row.config_json, dict) else json.loads(row.config_json)

    rotated_ct = config["hmac_secret"]
    assert rotated_ct != config.get("url")
    assert Fernet(new_key.encode()).decrypt(rotated_ct.encode()) == b"rotate-me-hmac"
    with pytest.raises(InvalidToken):
        Fernet(old_key.encode()).decrypt(rotated_ct.encode())

    # The narrow write left every NON-secret key byte-identical.
    assert config["url"] == "https://example.com/hook"
    assert config["event_filters"] == _EVENT_FILTERS
    assert config["work_item_ref_paths"] == [".agents"]
