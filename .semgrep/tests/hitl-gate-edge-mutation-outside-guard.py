from modulo.db.models.other_model import OtherModel
from modulo.db.models.pipeline_edge import PipelineEdge
from sqlalchemy import delete


def safe_delete_other_model(session, obj_id):
    # ok: hitl-gate-edge-mutation-outside-guard
    await session.execute(delete(OtherModel).where(OtherModel.id == obj_id))


def safe_type_reference() -> list[PipelineEdge]:
    # ok: hitl-gate-edge-mutation-outside-guard
    return []


def unsafe_delete_elsewhere(session, pipeline_id):
    # ruleid: hitl-gate-edge-mutation-outside-guard
    await session.execute(delete(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id))


def unsafe_aliased_delete_elsewhere(session, pipeline_id):
    # ruleid: hitl-gate-edge-mutation-outside-guard
    from sqlalchemy import delete as sa_delete

    await session.execute(sa_delete(PipelineEdge).where(PipelineEdge.pipeline_id == pipeline_id))


def unsafe_insert_elsewhere(session, org_id, pipeline_id):
    # ruleid: hitl-gate-edge-mutation-outside-guard
    edge = PipelineEdge(
        organisation_id=org_id,
        pipeline_id=pipeline_id,
        source_node_id="a",
        target_node_id="b",
        edge_type="normal",
    )
    session.add(edge)
