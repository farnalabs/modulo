def guarded(session, pid, org_id, nodes, edges, is_privileged):
    # ok: graph-write-missing-is-privileged
    return replace_pipeline_graph(
        session,
        pipeline_id=pid,
        org_id=org_id,
        nodes=nodes,
        edges=edges,
        is_privileged=is_privileged,
        caller_type="rest",
    )


def guarded_rollback(session, pid, sid):
    # ok: graph-write-missing-is-privileged
    return rollback_to_snapshot(session, pid, sid, is_privileged=True, caller_type="rest")


def missing_is_privileged(session, pid, org_id, nodes, edges):
    # ruleid: graph-write-missing-is-privileged
    return replace_pipeline_graph(
        session,
        pipeline_id=pid,
        org_id=org_id,
        nodes=nodes,
        edges=edges,
        caller_type="rest",
    )


def missing_caller_type(session, pid, org_id, nodes, edges):
    # ruleid: graph-write-missing-is-privileged
    return replace_pipeline_graph(
        session,
        pipeline_id=pid,
        org_id=org_id,
        nodes=nodes,
        edges=edges,
        is_privileged=True,
    )


def rollback_missing_both(session, pid, sid):
    # ruleid: graph-write-missing-is-privileged
    return rollback_to_snapshot(session, pid, sid)
