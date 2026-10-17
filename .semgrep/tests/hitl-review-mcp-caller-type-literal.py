def mcp_call_site(s, pid, org_id, nodes, edges, is_privileged):
    # ok: hitl-gate-mcp-caller-type-literal
    return replace_pipeline_graph(
        s,
        pipeline_id=pid,
        org_id=org_id,
        nodes=nodes,
        edges=edges,
        actor=GraphWriteActor(
            is_privileged=is_privileged,
            caller_type="mcp",
        ),
    )


def mcp_call_site_actor_arg_order(s, pid, org_id, nodes, edges, is_privileged):
    # ok: hitl-gate-mcp-caller-type-literal
    return replace_pipeline_graph(
        s,
        edges=edges,
        nodes=nodes,
        org_id=org_id,
        pipeline_id=pid,
        actor=GraphWriteActor(
            caller_type="mcp",
            account_id=None,
            is_privileged=is_privileged,
        ),
    )


def mcp_call_site_variable(s, pid, org_id, nodes, edges, is_privileged, caller):
    # ruleid: hitl-gate-mcp-caller-type-literal
    return replace_pipeline_graph(
        s,
        pipeline_id=pid,
        org_id=org_id,
        nodes=nodes,
        edges=edges,
        actor=GraphWriteActor(
            is_privileged=is_privileged,
            caller_type=caller,
        ),
    )


def mcp_call_site_derived(s, pid, org_id, nodes, edges, is_privileged):
    # ruleid: hitl-gate-mcp-caller-type-literal
    return replace_pipeline_graph(
        s,
        pipeline_id=pid,
        org_id=org_id,
        nodes=nodes,
        edges=edges,
        actor=GraphWriteActor(
            is_privileged=is_privileged,
            caller_type="mcp" if is_privileged else "rest",
        ),
    )
