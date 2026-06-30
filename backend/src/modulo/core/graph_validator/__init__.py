"""Graph validator — pre-run and on-save validation.

Checks:
1. Topology: no cycles, valid edge references, reachability, max nesting depth 3
2. Schema compatibility: output schema of each edge source matches input schema of target
3. Connector capability: bound connector instances are active and have required operations
4. Model backend health: pinned model backends exist and are active
5. Environment capability: bound EnvironmentProfile declares all agent required capabilities
6. Pre-run input payload compatibility with entry node schema
7. Node category: ``node_category_id`` references exist and are compatible with node type
"""

import uuid
from typing import Any

import jmespath
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.graph_validator._types import ValidationResult
from modulo.core.graph_validator.category_validator import validate_node_categories
from modulo.db.models.agent import Agent
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.environment_profile import EnvironmentProfile
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.schema import SchemaVersion

_SKIPPED_EDGE_TYPES = frozenset({"reject", "kickback"})
_JSON_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "object": dict,
    "array": list,
}


class GraphValidator:
    """Validates a PipelineSnapshot's graph before save or execution."""

    MAX_NESTING_DEPTH = 3

    async def validate(
        self,
        snapshot: PipelineSnapshot,
        session: AsyncSession,
    ) -> ValidationResult:
        return await self.validate_definition(
            snapshot.graph_json,
            session,
            connector_bindings=snapshot.connector_bindings_json,
            schema_pins=snapshot.schema_pins_json,
            model_backend_pins=snapshot.model_backend_pins_json,
            environment_profile_id=snapshot.environment_profile_id,
        )

    async def validate_definition(
        self,
        graph_json: dict[str, Any],
        session: AsyncSession,
        *,
        connector_bindings: list[dict[str, Any]] | None = None,
        schema_pins: list[dict[str, Any]] | None = None,
        model_backend_pins: list[dict[str, Any]] | None = None,
        environment_profile_id: uuid.UUID | None = None,
    ) -> ValidationResult:
        """Validate a live graph definition or an immutable snapshot.

        Returns warnings and errors. Errors block execution; warnings are advisory.
        """
        result = ValidationResult()

        self._check_topology(graph_json, result)
        if not result.is_valid:
            return result

        self._check_schema_compatibility(graph_json, schema_pins or [], result)
        await self._check_connector_bindings(connector_bindings or [], session, result)
        await self._check_model_backends(model_backend_pins or [], session, result)
        await self._check_environment_capabilities(
            environment_profile_id,
            graph_json,
            session,
            result,
        )

        await self._check_node_categories(graph_json, session, result)

        return result

    async def validate_for_run(
        self,
        snapshot: PipelineSnapshot,
        input_payload: dict[str, Any],
        session: AsyncSession,
    ) -> ValidationResult:
        """Pre-run validation — all save-time checks plus input schema checking.

        Returns errors only (no warnings). Any error blocks run start.
        """
        result = ValidationResult()

        # Topology: hard errors block immediately.
        self._check_topology(snapshot.graph_json, result)
        if not result.is_valid:
            return self._strip_warnings(result)

        # Schema compatibility (field-level).
        await self._check_schema_compatibility_deep(
            snapshot.graph_json,
            snapshot.schema_pins_json,
            session,
            result,
        )
        if not result.is_valid:
            return self._strip_warnings(result)

        # Input payload compatibility with entry node schema.
        await self._check_input_schema_compatibility(
            snapshot.graph_json,
            input_payload,
            snapshot.schema_pins_json,
            session,
            result,
        )
        if not result.is_valid:
            return self._strip_warnings(result)

        # Connector and backend checks.
        await self._check_connector_bindings(snapshot.connector_bindings_json, session, result)
        await self._check_model_backends(snapshot.model_backend_pins_json, session, result)

        # Environment capability check.
        await self._check_environment_capabilities(
            snapshot.environment_profile_id,
            snapshot.graph_json,
            session,
            result,
        )

        # Node category check.
        await self._check_node_categories(snapshot.graph_json, session, result)

        return self._strip_warnings(result)

    def _strip_warnings(self, result: ValidationResult) -> ValidationResult:
        """Return a copy containing only error-severity issues."""
        out = ValidationResult()
        for issue in result.issues:
            if issue.severity == "error":
                out.issues.append(issue)
        return out

    # ------------------------------------------------------------------
    # Topology
    # ------------------------------------------------------------------

    @staticmethod
    def _find_entry_candidates(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[str]:
        return [str(n["id"]) for n in nodes if str(n["id"]) not in {str(e["target"]) for e in edges}]

    def _check_topology(self, graph_json: dict[str, Any], result: ValidationResult) -> None:
        nodes: list[dict[str, Any]] = graph_json.get("nodes", [])
        edges: list[dict[str, Any]] = graph_json.get("edges", [])

        if not nodes:
            result.error("TOPOLOGY_NO_NODES", "Graph has no nodes")
            return

        node_ids = {str(n["id"]) for n in nodes}

        for edge in edges:
            src, tgt = str(edge["source"]), str(edge["target"])
            if src not in node_ids:
                result.error("TOPOLOGY_UNKNOWN_SOURCE", f"Edge source '{src}' is not a node")
            if tgt not in node_ids:
                result.error("TOPOLOGY_UNKNOWN_TARGET", f"Edge target '{tgt}' is not a node")

        if not result.is_valid:
            return

        # Validate conditional edge JMESPath expressions.
        self._check_condition_expressions(edges, result)

        # Determine forwarding edges (exclude kickback + reject from topology flow).
        flow_edges = [e for e in edges if e.get("type") not in _SKIPPED_EDGE_TYPES]

        # Entry node: no incoming forwarding edges
        entry_candidates = self._find_entry_candidates(nodes, flow_edges)
        if not entry_candidates:
            result.error("TOPOLOGY_CYCLE", "Graph has a cycle or no entry node")
            return

        # Build adjacency from forwarding edges.
        adj: dict[str, list[str]] = {str(n["id"]): [] for n in nodes}
        for edge in flow_edges:
            src, tgt = str(edge["source"]), str(edge["target"])
            adj[src].append(tgt)

        # Reachability BFS from entry over forwarding edges.
        visited: set[str] = set()
        queue = [entry_candidates[0]]
        while queue:
            nid = queue.pop()
            if nid in visited:
                continue
            visited.add(nid)
            queue.extend(adj.get(nid, []))

        for nid in sorted(node_ids - visited):
            result.warning(
                "TOPOLOGY_UNREACHABLE",
                f"Node '{nid}' is unreachable from entry node",
                node_id=nid,
            )

        # Nesting depth: longest path from entry node to any leaf.
        self._check_nesting_depth(adj, entry_candidates[0], result)

    def _check_nesting_depth(
        self,
        adj: dict[str, list[str]],
        entry_id: str,
        result: ValidationResult,
    ) -> None:
        """Compute longest path from entry to leaf via DFS. Error if > MAX_NESTING_DEPTH."""

        def _max_depth(node: str, visited: frozenset[str]) -> int:
            children = [c for c in adj.get(node, []) if c not in visited]
            if not children:
                return 1  # leaf counts as 1
            return 1 + max(_max_depth(c, visited | {node}) for c in children)

        depth = _max_depth(entry_id, frozenset())
        if depth > self.MAX_NESTING_DEPTH:
            result.error(
                "TOPOLOGY_NESTING_EXCEEDED",
                f"Graph nesting depth {depth} exceeds maximum {self.MAX_NESTING_DEPTH}",
            )

    # ------------------------------------------------------------------
    # Conditional edge expressions
    # ------------------------------------------------------------------

    def _check_condition_expressions(
        self,
        edges: list[dict[str, Any]],
        result: ValidationResult,
    ) -> None:
        """Validate JMESPath condition expressions on conditional edges
        and eval-reference conditions on HITL gates.

        Each conditional edge must have a non-empty ``condition_expression``
        that compiles as valid JMESPath.

        Each HITL gate with an ``eval_condition`` must have valid fields.
        """
        for edge in edges:
            self._validate_jmespath_conditional(edge, result)
            self._validate_hitl_eval_condition(edge, result)

    @staticmethod
    def _validate_jmespath_conditional(
        edge: dict[str, Any],
        result: ValidationResult,
    ) -> None:
        if edge.get("type") != "conditional":
            return
        src: str = str(edge.get("source", edge.get("source_node_id", "?")))
        expr: str | None = edge.get("condition_expression")
        if not expr or not expr.strip():
            result.error(
                "CONDITION_MISSING_EXPRESSION",
                f"Edge from '{src}': conditional edge requires a condition_expression",
                node_id=src,
            )
            return
        try:
            jmespath.compile(expr.strip())
        except Exception as exc:
            result.error(
                "CONDITION_INVALID_EXPRESSION",
                f"Edge from '{src}': invalid JMESPath expression: {exc}",
                node_id=src,
            )

    @staticmethod
    def _validate_hitl_eval_condition(
        edge: dict[str, Any],
        result: ValidationResult,
    ) -> None:
        """Validate eval_condition on a HITL gate config, if present."""
        hitl_config = edge.get("hitl_gate_config")
        if not hitl_config:
            return
        eval_cond = hitl_config.get("eval_condition")
        if not eval_cond:
            return
        src: str = str(edge.get("source", edge.get("source_node_id", "?")))
        eval_name: str | None = eval_cond.get("eval_name")
        if not eval_name or not eval_name.strip():
            result.error(
                "HITL_EVAL_CONDITION_MISSING_NAME",
                f"Edge from '{src}': eval_condition requires a non-empty eval_name",
                node_id=src,
            )
            return
        threshold = eval_cond.get("threshold")
        if threshold is None or not isinstance(threshold, (int, float)):
            result.error(
                "HITL_EVAL_CONDITION_INVALID_THRESHOLD",
                f"Edge from '{src}': eval_condition.threshold must be a number",
                node_id=src,
            )
            return
        if not (0.0 <= threshold <= 1.0):
            result.error(
                "HITL_EVAL_CONDITION_THRESHOLD_RANGE",
                f"Edge from '{src}': eval_condition.threshold must be between 0.0 and 1.0 (got {threshold})",
                node_id=src,
            )
            return
        valid_ops = {"lt", "gt", "lte", "gte", "eq", "neq"}
        operator: str | None = eval_cond.get("operator")
        if operator not in valid_ops:
            result.error(
                "HITL_EVAL_CONDITION_INVALID_OPERATOR",
                f"Edge from '{src}': eval_condition.operator must be one of {valid_ops} (got {operator!r})",
                node_id=src,
            )

    # ------------------------------------------------------------------
    # Schema compatibility
    # ------------------------------------------------------------------

    def _check_schema_compatibility(
        self,
        graph_json: dict[str, Any],
        schema_pins: list[dict[str, Any]],
        result: ValidationResult,
    ) -> None:
        # node_id -> direction -> schema_id
        schemas: dict[str, dict[str, str]] = {}
        for pin in schema_pins:
            nid = str(pin["node_id"])
            schemas.setdefault(nid, {})[pin["direction"]] = str(pin["schema_id"])

        for edge in graph_json.get("edges", []):
            if edge.get("type") in _SKIPPED_EDGE_TYPES:
                continue
            src, tgt = str(edge["source"]), str(edge["target"])
            src_out = schemas.get(src, {}).get("output")
            tgt_in = schemas.get(tgt, {}).get("input")

            if src_out is None or tgt_in is None:
                continue

            if src_out != tgt_in:
                result.error(
                    "SCHEMA_INCOMPATIBLE",
                    f"Edge {src}→{tgt}: output schema '{src_out}' != input schema '{tgt_in}'",
                    node_id=src,
                )

    async def _check_schema_compatibility_deep(
        self,
        graph_json: dict[str, Any],
        schema_pins: list[dict[str, Any]],
        session: AsyncSession,
        result: ValidationResult,
    ) -> None:
        """Field-level schema: output fields must exist in input with compatible types."""
        # node_id -> direction -> schema_id
        pins: dict[str, dict[str, str]] = {}
        for pin in schema_pins:
            nid = str(pin["node_id"])
            pins.setdefault(nid, {})[pin["direction"]] = str(pin["schema_id"])

        all_schema_ids: set[str] = set()
        for mapping in pins.values():
            for sid in mapping.values():
                all_schema_ids.add(sid)

        if not all_schema_ids:
            return

        definitions = await self._resolve_schema_definitions(all_schema_ids, session)

        for edge in graph_json.get("edges", []):
            if edge.get("type") in _SKIPPED_EDGE_TYPES:
                continue
            src, tgt = str(edge["source"]), str(edge["target"])
            src_out_id = pins.get(src, {}).get("output")
            tgt_in_id = pins.get(tgt, {}).get("input")

            if src_out_id is None or tgt_in_id is None:
                continue

            out_def = definitions.get(src_out_id, {})
            in_def = definitions.get(tgt_in_id, {})

            self._check_field_compatibility(src, tgt, out_def, in_def, result)

    def _check_field_compatibility(
        self,
        src: str,
        tgt: str,
        out_def: dict[str, Any],
        in_def: dict[str, Any],
        result: ValidationResult,
    ) -> None:
        out_props = out_def.get("properties", {}) if isinstance(out_def, dict) else {}
        in_props = in_def.get("properties", {}) if isinstance(in_def, dict) else {}

        for field_name, out_field in out_props.items():
            if not isinstance(out_field, dict):
                continue
            in_field = in_props.get(field_name)
            if in_field is None:
                result.error(
                    "SCHEMA_MISSING_FIELD",
                    f"Edge {src}→{tgt}: output field '{field_name}' not found in input schema",
                    node_id=src,
                )
                continue
            if isinstance(in_field, dict) and isinstance(out_field, dict):
                out_type = out_field.get("type")
                in_type = in_field.get("type")
                if out_type and in_type and out_type != in_type:
                    result.error(
                        "SCHEMA_FIELD_TYPE_MISMATCH",
                        f"Edge {src}→{tgt}: field '{field_name}' type '{out_type}' != input type '{in_type}'",
                        node_id=src,
                    )

    async def _resolve_schema_definitions(
        self,
        schema_ids: set[str],
        session: AsyncSession,
    ) -> dict[str, dict[str, Any]]:
        """Fetch the latest published definition_json for each schema_id.

        Returns dict[schema_id, definition_json].
        """
        if not schema_ids:
            return {}

        uuids = {uuid.UUID(s) for s in schema_ids}
        rows = (
            (
                await session.execute(
                    select(SchemaVersion).where(
                        SchemaVersion.schema_id.in_(uuids),
                        SchemaVersion.published.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        latest: dict[uuid.UUID, dict[str, Any]] = {}
        for row in rows:
            existing = latest.get(row.schema_id)
            if existing is None or row.version_number > existing.get("_version_number", -1):
                defn = dict(row.definition_json) if row.definition_json else {}
                defn["_version_number"] = row.version_number
                latest[row.schema_id] = defn

        return {str(k): v for k, v in latest.items()}

    async def _check_input_schema_compatibility(
        self,
        graph_json: dict[str, Any],
        input_payload: dict[str, Any],
        schema_pins: list[dict[str, Any]],
        session: AsyncSession,
        result: ValidationResult,
    ) -> None:
        """Validate trigger input payload is compatible with entry node's input schema."""
        nodes: list[dict[str, Any]] = graph_json.get("nodes", [])
        edges: list[dict[str, Any]] = graph_json.get("edges", [])
        flow_edges = [e for e in edges if e.get("type") not in _SKIPPED_EDGE_TYPES]
        entry_candidates = self._find_entry_candidates(nodes, flow_edges)
        if not entry_candidates:
            result.error("INPUT_NO_ENTRY", "Cannot determine entry node for input validation")
            return
        entry_id = entry_candidates[0]

        entry_pins = [p for p in schema_pins if str(p.get("node_id")) == entry_id and p.get("direction") == "input"]
        if not entry_pins:
            return

        entry_schema_id = str(entry_pins[0]["schema_id"])
        definitions = await self._resolve_schema_definitions({entry_schema_id}, session)
        entry_def = definitions.get(entry_schema_id, {})

        entry_props = entry_def.get("properties", {}) if isinstance(entry_def, dict) else {}

        required_fields: set[str] = set(entry_def.get("required", []))

        for field_name, field_def in entry_props.items():
            if not isinstance(field_def, dict):
                continue
            if field_name in required_fields and field_name not in input_payload:
                result.error(
                    "INPUT_MISSING_FIELD",
                    f"Input payload missing required field '{field_name}' for entry node '{entry_id}'",
                    node_id=entry_id,
                )
                continue
            if field_name not in input_payload:
                continue
            expected_type = field_def.get("type")
            if expected_type and not isinstance(input_payload[field_name], _JSON_TYPE_MAP.get(expected_type, object)):
                actual_type = type(input_payload[field_name]).__name__
                result.error(
                    "INPUT_FIELD_TYPE_MISMATCH",
                    f"Input field '{field_name}' expected type '{expected_type}', got '{actual_type}'",
                    node_id=entry_id,
                )

    # ------------------------------------------------------------------
    # Connector bindings
    # ------------------------------------------------------------------

    async def _check_connector_bindings(
        self,
        bindings: list[dict[str, Any]],
        session: AsyncSession,
        result: ValidationResult,
    ) -> None:
        if not bindings:
            return

        instance_ids = {uuid.UUID(str(b["connector_instance_id"])) for b in bindings}
        rows = (
            (await session.execute(select(ConnectorInstance).where(ConnectorInstance.id.in_(instance_ids))))
            .scalars()
            .all()
        )
        found: dict[uuid.UUID, ConnectorInstance] = {r.id: r for r in rows}

        for binding in bindings:
            node_id: str | None = str(binding["node_id"]) if binding.get("node_id") else None
            cid = uuid.UUID(str(binding["connector_instance_id"]))
            instance = found.get(cid)

            if instance is None:
                result.error("CONNECTOR_NOT_FOUND", f"Connector instance {cid} not found", node_id)
                continue

            if instance.status != "active":
                result.error(
                    "CONNECTOR_INACTIVE",
                    f"Connector {cid} ({instance.name!r}) has status {instance.status!r}",
                    node_id,
                )

            required_ops: list[str] = binding.get("required_operations", [])
            allowed_ops: list[str] = instance.allowed_operations or []
            missing = [op for op in required_ops if op not in allowed_ops]
            if missing:
                result.error(
                    "CONNECTOR_MISSING_OPERATIONS",
                    f"Connector {cid} missing operations: {missing}",
                    node_id,
                )

    # ------------------------------------------------------------------
    # Model backend health
    # ------------------------------------------------------------------

    async def _check_model_backends(
        self,
        pins: list[dict[str, Any]],
        session: AsyncSession,
        result: ValidationResult,
    ) -> None:
        if not pins:
            return

        backend_ids = {uuid.UUID(str(p["model_backend_id"])) for p in pins}
        rows = (await session.execute(select(ModelBackend).where(ModelBackend.id.in_(backend_ids)))).scalars().all()
        found: dict[uuid.UUID, ModelBackend] = {r.id: r for r in rows}

        for pin in pins:
            node_id: str | None = str(pin["node_id"]) if pin.get("node_id") else None
            bid = uuid.UUID(str(pin["model_backend_id"]))
            backend = found.get(bid)

            if backend is None:
                result.error("MODEL_BACKEND_NOT_FOUND", f"Model backend {bid} not found", node_id)
                continue

            if backend.status != "active":
                result.error(
                    "MODEL_BACKEND_INACTIVE",
                    f"Model backend {bid} ({backend.name!r}) has status {backend.status!r}",
                    node_id,
                )
                continue

            if backend.last_health_check_error:
                result.error(
                    "MODEL_BACKEND_UNHEALTHY",
                    f"Model backend '{backend.name}' (id={bid}) is unhealthy: {backend.last_health_check_error}",
                    node_id,
                )

    # ------------------------------------------------------------------
    # Environment capabilities
    # ------------------------------------------------------------------

    async def _check_environment_capabilities(
        self,
        environment_profile_id: uuid.UUID | None,
        graph_json: dict[str, Any],
        session: AsyncSession,
        result: ValidationResult,
    ) -> None:
        """Check that the bound EnvironmentProfile covers all agent capabilities.

        Hard-block if any agent requires a capability the profile does not declare.
        Skipped if no environment_profile_id is set on the snapshot.
        """
        if environment_profile_id is None:
            return

        profile = await session.get(EnvironmentProfile, environment_profile_id)
        if profile is None:
            result.error(
                "ENV_PROFILE_NOT_FOUND",
                f"EnvironmentProfile {environment_profile_id} not found",
            )
            return

        agent_ids: set[uuid.UUID] = set()
        for node in graph_json.get("nodes", []):
            raw = node.get("agent_id")
            if raw is not None:
                try:
                    agent_ids.add(uuid.UUID(str(raw)))
                except (ValueError, TypeError):
                    continue

        if not agent_ids:
            return

        rows = (await session.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars().all()

        profile_caps: set[str] = set(profile.capabilities or [])

        for agent in rows:
            required: list[str] = agent.required_environment_capabilities or []
            if not required:
                continue
            missing = [c for c in required if c not in profile_caps]
            if missing:
                result.error(
                    "ENV_MISSING_CAPABILITIES",
                    f"Agent '{agent.name}' requires capabilities {missing}"
                    f" not declared by EnvironmentProfile '{profile.name}'",
                )

    # ------------------------------------------------------------------
    # Node categories
    # ------------------------------------------------------------------

    async def _check_node_categories(
        self,
        graph_json: dict[str, Any],
        session: AsyncSession,
        result: ValidationResult,
    ) -> None:
        """Check that all ``node_category_id`` references are valid.

        Delegates to ``validate_node_categories`` for the actual check
        and merges results into the running result.
        """
        cat_result = await validate_node_categories(graph_json, session)
        result.issues.extend(cat_result.issues)
