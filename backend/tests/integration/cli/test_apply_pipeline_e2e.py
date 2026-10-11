"""FAR-1109: declarative node-attached policy gates end-to-end (spec §4.3).

Real Postgres (testcontainers) + real migrations + the real FastAPI routes.

The declarative source of truth for a pipeline's policy gates is its graph's
``policy_gate`` blocks. The runtime engine, however, resolves gating ONLY from
``policy_gates`` rows joined to evals (``PipelineExecutor._load_eval_defs_for_pipeline``)
— a block stored in ``graph_nodes_json`` governs nothing on its own. This module
proves the SERVER graph-write normalisation translates each declared block into
the gate-row lifecycle:

* a YAML-declared gate creates a live ``PolicyGate`` row and the executor's own
  eval-def build resolves ``failure_behaviour="block"`` — the run is ENFORCED;
* re-applying the identical declaration is idempotent (same row, same version);
* removing the block SOFT-deletes the row (never hard-deletes) and the executor
  no longer resolves a governing gate — the run stops being blocked;
* re-adding the block yields exactly one live gate while the previously
  soft-deleted row remains soft-deleted (spec criterion 13).

The YAML is loaded through the REAL CLI loader/model and the graph is built by
the REAL CLI normaliser (``resolve_graph``), then written through the exact
``PATCH /api/v1/pipelines/{id}`` ``graph_json`` path ``modulo apply`` uses. (The
CLI's HTTP transport is a synchronous httpx client and cannot attach to the
in-process ASGI app, so the route is driven by the ASGI client with the CLI's
own normalised payload.)
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from modulo.auth.jwt import create_access_token
from modulo.cli.apply.loader import parse_apply_documents
from modulo.cli.apply.pipeline_apply import resolve_graph
from modulo.core.pipeline_engine.executor import PipelineExecutor
from modulo.db.crud.schema import create_schema, create_schema_version

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32
_NODE_ID = uuid.UUID("11111111-1111-1111-1111-1111a1109101")


def _auth_headers(org_id: uuid.UUID, account_id: uuid.UUID, role: str = "admin") -> dict[str, str]:
    token = create_access_token(
        subject=f"user-{account_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(account_id),
        org_role=role,
        client_kind="browser",
    )
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Seeding (superuser engine; ASGI route calls run through the app role)
# ---------------------------------------------------------------------------


async def _seed_org(db_engine: AsyncEngine, name: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": name, "slug": f"{name}-{org_id.hex[:8]}"},
        )
    return org_id


async def _seed_admin(db_engine: AsyncEngine, org_id: uuid.UUID) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
                "VALUES (:id, :email, :name, 'hash', 'local', true)"
            ),
            {
                "id": str(account_id),
                "email": f"far1109-{account_id.hex[:10]}@example.com",
                "name": "FAR-1109 Admin",
            },
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:mid, :aid, :oid, 'admin')"
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )
    return account_id


async def _seed_schema(db_engine: AsyncEngine, org_id: uuid.UUID, account_id: uuid.UUID) -> uuid.UUID:
    """A committed schema the ``manual`` node's output can point at (FAR-889)."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.organisation_id', :oid, true)"),
            {"oid": str(org_id)},
        )
        schema = await create_schema(
            session,
            org_id=org_id,
            name=f"Far1109Schema-{uuid.uuid4().hex[:6]}",
            account_id=account_id,
        )
        await create_schema_version(
            session,
            org_id=org_id,
            schema_id=schema.id,
            version="1.0",
            version_number=1,
            definition_json={"type": "object"},
            account_id=account_id,
        )
        return schema.id


# ---------------------------------------------------------------------------
# CLI-side config (YAML -> real loader -> real normaliser)
# ---------------------------------------------------------------------------


def _config_yaml(
    *,
    pipeline_name: str,
    node_id: uuid.UUID,
    schema_id: uuid.UUID,
    eval_id: uuid.UUID | None,
    gate_action: str | None = None,
) -> str:
    gate = ""
    if eval_id is not None and gate_action is not None:
        gate = f'\n            policy_gate:\n              action: {gate_action}\n              eval_id: "{eval_id}"'
    return (
        "\n"
        "api_version: modulo.dev/v1\n"
        "entities:\n"
        "  pipelines:\n"
        f"    - name: {pipeline_name}\n"
        "      graph:\n"
        "        nodes:\n"
        f'          - id: "{node_id}"\n'
        "            node_type: manual\n"
        '            label: "FAR-1109 step"\n'
        "            position: {x: 0, y: 0}\n"
        f'            output_schema_id: "{schema_id}"{gate}\n'
        "        edges: []\n"
    )


def _graph_payload(*, pipeline_name: str, node_id: uuid.UUID, schema_id: uuid.UUID, yaml_text: str) -> dict:
    config = parse_apply_documents(yaml_text)
    entity = next(e for e in config.entities.pipelines if e.name == pipeline_name)
    return resolve_graph(entity, {})


# ---------------------------------------------------------------------------
# Runtime resolution (the authoritative gate read)
# ---------------------------------------------------------------------------


async def _resolve_runtime_gate(
    db_engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    node_id: uuid.UUID,
) -> tuple[object, object]:
    """Return the ``(EvalDefDTO, node_key)`` the run's build path resolves.

    Uses the executor's own loader + build (the ONE authoritative gate read),
    so the assertion is about what a run would actually enforce, not a
    hand-built fixture.
    """
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        rows = await PipelineExecutor._load_eval_defs_for_pipeline(None, session, pipeline_id)  # type: ignore[arg-type]
        by_node = PipelineExecutor._build_eval_defs_by_node(rows, org_id, pipeline_id, None)
    key = str(node_id)
    assert by_node.get(key), f"runtime resolved no eval for node {key}"
    return by_node[key][0], key


async def _gate_rows(db_engine: AsyncEngine, eval_id: uuid.UUID) -> list:
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT id, action, version, deleted_at IS NOT NULL AS is_deleted "
                "FROM policy_gates WHERE eval_id = :e ORDER BY created_at"
            ),
            {"e": str(eval_id)},
        )
        return result.all()


async def _scenario(integration_client: AsyncClient, db_engine: AsyncEngine) -> dict:
    """Seed org/admin/schema, create the pipeline + eval via the API."""
    org_id = await _seed_org(db_engine, f"far1109-{uuid.uuid4().hex[:8]}")
    admin_id = await _seed_admin(db_engine, org_id)
    headers = _auth_headers(org_id, admin_id)
    schema_id = await _seed_schema(db_engine, org_id, admin_id)

    pipeline_name = f"far1109-pipeline-{uuid.uuid4().hex[:6]}"
    created = await integration_client.post("/api/v1/pipelines", json={"name": pipeline_name}, headers=headers)
    assert created.status_code == 201, created.text
    pipeline_id = uuid.UUID(created.json()["id"])

    # POST /evals persists an eval bound to the node. It also creates a
    # default `warn` gate, which the test removes so the first apply is proven
    # to CREATE the gate row (not merely inherit one).
    eval_resp = await integration_client.post(
        "/api/v1/evals",
        json={
            "pipeline_id": str(pipeline_id),
            "node_id": str(_NODE_ID),
            "name": f"far1109-eval-{uuid.uuid4().hex[:6]}",
            "eval_type": "regex",
            "config_json": {"field": "output", "pattern": ".*"},
        },
        headers=headers,
    )
    assert eval_resp.status_code == 201, eval_resp.text
    eval_id = uuid.UUID(eval_resp.json()["id"])

    removed = await integration_client.delete(f"/api/v1/evals/{eval_id}/policy-gate", headers=headers)
    assert removed.status_code == 204, removed.text

    return {
        "org_id": org_id,
        "admin_id": admin_id,
        "headers": headers,
        "schema_id": schema_id,
        "pipeline_name": pipeline_name,
        "pipeline_id": pipeline_id,
        "eval_id": eval_id,
    }


async def _write_graph(integration_client: AsyncClient, scenario: dict, yaml_text: str) -> None:
    graph = _graph_payload(
        pipeline_name=scenario["pipeline_name"],
        node_id=_NODE_ID,
        schema_id=scenario["schema_id"],
        yaml_text=yaml_text,
    )
    response = await integration_client.patch(
        f"/api/v1/pipelines/{scenario['pipeline_id']}",
        json={"graph_json": graph},
        headers=scenario["headers"],
    )
    assert response.status_code == 200, response.text
    # The block must survive the server read path (round-trip).
    stored = await integration_client.get(
        f"/api/v1/pipelines/{scenario['pipeline_id']}/graph", headers=scenario["headers"]
    )
    assert stored.status_code == 200, stored.text
    policy_gate = graph["nodes"][0].get("policy_gate")
    stored_node = next(n for n in stored.json()["nodes"] if str(n["id"]) == str(_NODE_ID))
    if policy_gate is None:
        assert stored_node.get("policy_gate") is None
    else:
        assert stored_node["policy_gate"] == policy_gate, stored_node.get("policy_gate")


# ---------------------------------------------------------------------------
# Criterion 9/11/13 — declared gate enforces; removed gate stops governing
# ---------------------------------------------------------------------------


async def test_declared_gate_governs_run_then_removal_stops_governing(
    integration_client: AsyncClient, db_engine: AsyncEngine
) -> None:
    """A YAML-declared gate creates a live row that ENFORCES; removing it
    soft-deletes the row and the run stops being governed."""
    scenario = await _scenario(integration_client, db_engine)
    eval_id = scenario["eval_id"]
    pipeline_id = scenario["pipeline_id"]

    # --- baseline: no live gate governs the run ---------------------------
    assert all(row.is_deleted for row in await _gate_rows(db_engine, eval_id))
    baseline_dto, _ = await _resolve_runtime_gate(db_engine, scenario["org_id"], pipeline_id, _NODE_ID)
    assert baseline_dto.failure_behaviour == "warn"
    assert baseline_dto.policy_gate_id is None

    # --- apply a YAML-declared gate -> live row + enforcement -------------
    declared = _config_yaml(
        pipeline_name=scenario["pipeline_name"],
        node_id=_NODE_ID,
        schema_id=scenario["schema_id"],
        eval_id=eval_id,
        gate_action="block",
    )
    await _write_graph(integration_client, scenario, declared)

    live = [row for row in await _gate_rows(db_engine, eval_id) if not row.is_deleted]
    assert len(live) == 1, f"declared gate must create exactly one live row: {live}"
    assert live[0].action == "block"
    block_gate_id = live[0].id

    governed_dto, _ = await _resolve_runtime_gate(db_engine, scenario["org_id"], pipeline_id, _NODE_ID)
    assert governed_dto.failure_behaviour == "block"
    assert governed_dto.policy_gate_id == live[0].id

    # --- re-apply the identical declaration -> idempotent -----------------
    await _write_graph(integration_client, scenario, declared)
    after = await _gate_rows(db_engine, eval_id)
    live_after = [row for row in after if not row.is_deleted]
    assert len(live_after) == 1, after
    assert live_after[0].id == live[0].id, "re-apply must not recreate the row"
    assert live_after[0].version == live[0].version, "re-apply must not bump the version"

    # --- remove the block -> soft-delete + no governance ------------------
    removed_yaml = _config_yaml(
        pipeline_name=scenario["pipeline_name"],
        node_id=_NODE_ID,
        schema_id=scenario["schema_id"],
        eval_id=None,
    )
    await _write_graph(integration_client, scenario, removed_yaml)

    rows = await _gate_rows(db_engine, eval_id)
    by_id = {row.id: row for row in rows}
    assert block_gate_id in by_id, "removal must SOFT-delete, never hard-delete, the gate row"
    assert by_id[block_gate_id].is_deleted, "the removed gate row must be soft-deleted"
    assert not any(not row.is_deleted for row in rows), f"no live gate may remain: {rows}"

    ungoverned_dto, _ = await _resolve_runtime_gate(db_engine, scenario["org_id"], pipeline_id, _NODE_ID)
    assert ungoverned_dto.failure_behaviour == "warn"
    assert ungoverned_dto.policy_gate_id is None

    # --- re-add -> exactly one live gate, old row still soft-deleted ------
    await _write_graph(integration_client, scenario, declared)
    final_rows = await _gate_rows(db_engine, eval_id)
    final_live = [row for row in final_rows if not row.is_deleted]
    assert len(final_live) == 1, f"re-add must leave exactly one live gate: {final_rows}"
    assert final_live[0].action == "block"
    assert final_live[0].id != block_gate_id, "re-add must create a fresh row, not undelete the old one"
    assert any(row.id == block_gate_id and row.is_deleted for row in final_rows), (
        "the previously soft-deleted gate must remain soft-deleted"
    )

    regoverned_dto, _ = await _resolve_runtime_gate(db_engine, scenario["org_id"], pipeline_id, _NODE_ID)
    assert regoverned_dto.failure_behaviour == "block"
    assert regoverned_dto.policy_gate_id == final_live[0].id
