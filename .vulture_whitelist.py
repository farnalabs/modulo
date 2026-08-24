# Vulture whitelist: framework-required symbols that vulture cannot resolve.
#
# These are NOT dead code in the sense the gate exists to catch — removing them
# breaks framework contracts, OR they are genuinely dead in production but
# referenced by tests (which we never delete). Each entry carries a comment
# grouping it by contract. Static analysis (vulture) reports them as dead
# while they are in fact load-bearing or test-referenced.
#
# Mechanism: vulture's documented whitelist is a Python file passed as an
# additional PATH argument. Names reach vulture's `used_names` set (which is
# what suppresses "unused" reports) when the whitelist module declares them in
# an `__all__` list — vulture special-cases `__all__ = [...]` in visit_Assign
# and adds every string element to used_names. A plain `ignored = [...]` list
# does NOT suppress anything; only `__all__` (or bare Load references) does.
# (Verified empirically against vulture 2.16: stub `def`/`class` definitions
# do NOT suppress findings — they only enter defined_funcs/defined_classes.)
#
# The names are intentionally undefined here (they exist in backend/src) —
# that is the whole point of a whitelist — so suppress ruff's F822 for this
# file. RUF022 (__all__ not sorted) is also suppressed: the entries are
# deliberately grouped by contract category (see the section comments), not
# sorted alphabetically, and an isort-style sort would interleave the
# categories and destroy the grouping. This file lives at the repo root,
# outside the backend/ ruff scan scope; the directive only protects against a
# future root-level lint.
# ruff: noqa: F822, RUF022
__all__ = [
    # --- TYPE_CHECKING / string-annotation contracts (kept) ---
    "CursorResult",  # TYPE_CHECKING imports used as string annotations
    "Dialect",  # TYPE_CHECKING import used in a cast string annotation
    "compiler",  # SQLAlchemy @compiles() hook callback params
    "element",  # SQLAlchemy @compiles() hook callback params
    "input_str",  # LangChain callback interface params
    "inputs",  # LangChain callback interface params
    "q_or_none",  # documented placeholder param (seam parity)
    "version_id",  # FastAPI path params (route shape, unused by design)
    # --- Framework-registered classes (registered lazily via __getattr__ in model_backends/__init__.py) ---
    "AzureOpenAIBackend",
    "BedrockBackend",
    "CohereBackend",
    "JanBackend",
    "LLamaCppBackend",
    "LmStudioBackend",
    "LocalAIBackend",
    "MistralBackend",
    "OllamaBackend",
    "TgiBackend",
    "VertexAIBackend",
    "VllmBackend",
    "WatsonXBackend",
    # --- Remy route dynamic backend dispatch (remy.py _BACKEND_IMPORTS, resolved via importlib + getattr) ---
    "Ai21Backend",
    "AnthropicBackend",
    "DeepSeekBackend",
    "FireworksBackend",
    "GeminiBackend",
    "GrokBackend",
    "GroqBackend",
    "OpenRouterBackend",
    "PerplexityBackend",
    "QwenBackend",
    "TogetherAIBackend",
    # --- Framework contracts (string-cast Protocol / config-driven registries) ---
    "_TaskGroupSessionManager",
    "RepositoryHub",
    "AdvisoryLockService",
    # --- Connector test doubles (referenced only by backend/tests) ---
    "_AzurePipelinesTestDouble",
    "_BuildkiteTestDouble",
    "_CircleCITestDouble",
    "_GitHubActionsTestDouble",
    "_GitLabCITestDouble",
    "_JenkinsTestDouble",
    "_TeamCityTestDouble",
    # --- Known dead in production, referenced only by tests (kept — see comments) ---
    "LicenseKeyTier",  # test-support PlanContext double — used by BDD steps to fake licensed plans
    "circuit_state",  # test-consumed circuit-breaker observability API (health_check deliberately bypasses the breaker)
    "get_api_key_role_cap_count",  # test-support diagnostic getter for API-key role-cap security counter
    # runs.py wrapper kept for test_prompt_reveal (reveal_node_prompt uses _build_messages)
    "_build_messages_from_agent_and_state",
    # --- Service methods exercised by tests / framework wiring (no direct prod call site vulture can see) ---
    "get_override",
    "expire_stale",
    "get_gate",
    "list_overdue",
    "count_overdue",
    "cleanup_stale",
    "get_with_rotation",
    "mark_unhealthy",
    "register_connector_type",
    "register_model_backend",
    "update_config",
    "check_access",
    "get_and_clear_permission_decision",
    "clear_all_overrides",
    "unregister",
    "create_lease",
    "destroy_lease",
    "workspace_health",
    "write_file",
    "read_file",
    "list_types",
    "set_session",
    "acquire_lock",
    "release_lock",
    "try_acquire",
    "get_migration",
    "apply_partial",
    "describe_partial_chain",
    "dry_run_partial",
    "apply",
    "describe_chain",
    "list_migrations",
    # --- Auth / feature helpers referenced only by tests ---
    "get_effective_team_role",
    "team_role_level",
    "refresh_access_token",
    "rotate_oauth_token_family",
    "blacklist_oauth_token_family",
    "clear_jwks_cache",
    "verify_id_token_with_discovery",
    "_decode_id_token_claims",
    "_parse_saml_datetime",
    "_require_runner",
    "build_tool_definitions_for_text",
    # --- FAR-374 Phase 1 eval-suite feature flag (referenced by tests; production
    #     callers that read it land in later phases, so vulture cannot see a
    #     prod call site yet) ---
    "eval_maturity_enabled",
    # --- CRUD functions referenced only by tests ---
    "delete_composite_template",
    "upsert_daily_run_count",
    "get_daily_run_counts",
    "get_org_spend_total",
    "delete_node_category",
    "get_child_nodes",
    "set_parent_node",
    "update_membership_role",
    "delete_set",
    "delete_pipeline",
    "list_abuse_reports",
    "review_abuse_report",
    "update_run_outputs",
    "transition_run",
    "cancel_run",
    "get_feature_flag",
    "get_or_create_family",
    "is_family_blacklisted",
    "get_effective_setting",
    "extract_orm_entity",
    # --- Error-tracking / alerting helpers referenced only by tests ---
    "configure_forwarders",
    "emit_signal_event",
    "emit_retry_deferred_alert",
    "emit_alert_resolved",
    "seed_default_alert_rules",
    "tombstone_default_rule",
    "clear_default_rule_tombstone",
    "restore_default_alert_rules_for_org",
    "record_settings_warning",
    # --- Reporting / polling / scheduler helpers referenced only by tests ---
    "_set_test_engine",
    "_fire_scheduled_report",
    "_update_next_fire",
    "_update_next_fire_no_last",
    "_daily_spend_limit_reached",
    "_test_reset_connections",
    "_resolve_log_level",
    "execute_composite_with_retry",
    "clean_legacy_content",
    "is_retryable",
    "configure_registry",
    "extract_output_json",
    "reconcile_noop_evidence",
    "get_pricing",
    "apply_permission_mode_preset",
    "create_local_provider_from_env",
    "runs_settings",
    "staging_runs_settings",
    "staging_system_settings",
    "validate_node_category",
    "verify_write_scopes",
    "fetch_blob",  # LibraryClient.fetch_blob (FAR-363) - public blob-fetch API exercised by tests/unit/library_sync
    "_aggregate_sandbox_cost",
    "_compute_token_costs",
    # --- Observability / read-model properties referenced only by tests ---
    "active_run_count",
    "buffered_count",
    "subscriber_count",
    "is_shutting_down",
    "connector_types",
    "backend_providers",
    "entry_point_errors",
    "locks",
    "effective_max_rate_usd",
    # PipelineResponse field set on 9.3 ownership transfer; read via JSON
    # serialization by the frontend
    "connector_rebind_required",
    # --- ORM / framework attributes (SQLAlchemy mapped attrs, protocol attrs, dataclass-like slots) ---
    "blacklisted_at",
    "resolved_at",
    "reviewed_at",
    "deprecated",
    "deprecated_at",
    "delivered_at",
    "checkpointer",
    "input_hash",
    "deleted_by",  # eval_definitions write-only audit column (set on soft-delete, FAR-309 PR B)
    "catalog_json",  # LibrarySyncState (FAR-363) write-only cache column - consumed by the future library browser
    "last_synced_at",  # LibrarySyncState (FAR-363) write-only sync-stamp column
    "last_success_at",  # LibrarySyncState (FAR-363) write-only success-stamp column
    "_superseded",
    "_task_group",
    "lifespan_context",
    "_tier",
    "_max_concurrency",
    "_model_id",
    "_config",
    "_creds",
    "_jobs",
    "_nodes",
    "_projects",
    "_build_types",
    # --- Sandbox exception classes referenced via LEGACY_ALIASES string lookup ---
    "SandboxRateLimitedError",
    "created_by_me",
    "circuit_breaker_tripped_at",
    "last_event_id",
    # --- Product analytics — public API used in tests ---
    "get_instance_id",
    # --- Product analytics partner carve-out (FAR-354, used by FAR-361 enforcement) ---
    "is_partner_carve_out_active",
    "is_license_enforcement_enabled",
    # --- Product analytics enforcement (FAR-361) ---
    "is_enforcement_active",
    "should_degrade_to_community",
]
