export interface paths {
  '/api/v1/dashboard/summary': {
    get: {
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['DashboardSummaryResponse']
          }
        }
      }
    }
  }
  '/api/v1/admin/teams': {
    get: {
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['AdminTeamListResponse']
          }
        }
      }
    }
    post: {
      requestBody: {
        content: {
          'application/json': components['schemas']['AdminCreateTeamRequest']
        }
      }
      responses: {
        201: {
          content: {
            'application/json': components['schemas']['AdminCreateTeamResponse']
          }
        }
      }
    }
  }
  '/api/v1/admin/teams/{team_id}': {
    put: {
      parameters: {
        path: { team_id: string }
      }
      requestBody: {
        content: {
          'application/json': components['schemas']['AdminUpdateTeamRequest']
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['AdminTeamItem']
          }
        }
      }
    }
    delete: {
      parameters: {
        path: { team_id: string }
      }
      responses: {
        204: { description: 'No content' }
      }
    }
  }
  '/api/v1/teams/{team_id}/members': {
    get: {
      parameters: {
        path: { team_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['MembershipListResponse']
          }
        }
      }
    }
    post: {
      parameters: {
        path: { team_id: string }
      }
      requestBody: {
        content: {
          'application/json': components['schemas']['AddMemberRequest']
        }
      }
      responses: {
        201: {
          content: {
            'application/json': components['schemas']['MembershipResponse']
          }
        }
      }
    }
  }
  '/api/v1/teams/{team_id}/members/{membership_id}': {
    delete: {
      parameters: {
        path: { team_id: string; membership_id: string }
      }
      responses: {
        204: { description: 'No content' }
      }
    }
  }
  '/api/v1/admin/users': {
    get: {
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['AdminUserListResponse']
          }
        }
      }
    }
  }
  '/api/v1/admin/sso/providers': {
    get: {
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['SsoProviderResponse'][]
          }
        }
      }
    }
    post: {
      requestBody: {
        content: {
          'application/json': components['schemas']['SsoProviderCreate']
        }
      }
      responses: {
        201: {
          content: {
            'application/json': components['schemas']['SsoProviderResponse']
          }
        }
      }
    }
  }
  '/api/v1/admin/sso/providers/{provider_id}': {
    put: {
      parameters: {
        path: { provider_id: string }
      }
      requestBody: {
        content: {
          'application/json': components['schemas']['SsoProviderUpdate']
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['SsoProviderResponse']
          }
        }
      }
    }
    delete: {
      parameters: {
        path: { provider_id: string }
      }
      responses: {
        204: { description: 'No content' }
      }
    }
  }
  '/api/v1/admin/sso/providers/{provider_id}/toggle': {
    put: {
      parameters: {
        path: { provider_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['SsoProviderResponse']
          }
        }
      }
    }
  }
  '/api/v1/admin/sso/providers/{provider_id}/test': {
    post: {
      parameters: {
        path: { provider_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['SsoProviderTestResult']
          }
        }
      }
    }
  }
  '/api/v1/admin/audit': {
    get: {
      parameters: {
        query: {
          cursor?: string
          limit?: number
          event_type?: string
          user_id?: string
          entity_type?: string
          from_date?: string
          to_date?: string
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['AuditLogListResponse']
          }
        }
      }
    }
  }
  '/api/v1/admin/rate-limits': {
    get: {
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['RateLimitStatusResponse']
          }
        }
      }
    }
    put: {
      requestBody: {
        content: {
          'application/json': components['schemas']['RateLimitUpdateRequest']
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['RateLimitStatusResponse']
          }
        }
      }
    }
  }
  '/api/v1/admin/audit/export': {
    get: {
      parameters: {
        query: {
          page?: number
          page_size?: number
          event_type?: string
          user_id?: string
          entity_type?: string
          from_date?: string
          to_date?: string
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['AuditExportResponse']
          }
        }
      }
    }
  }
  '/api/v1/changelog': {
    get: {
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['ChangelogEntry'][]
          }
        }
      }
    }
  }
  '/api/v1/connectors': {
    get: {
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['ConnectorListResponse']
          }
        }
      }
    }
  }
  '/api/v1/schemas/infer': {
    post: {
      requestBody: {
        content: {
          'application/json': components['schemas']['SchemaInferRequest']
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['SchemaInferResponse']
          }
        }
      }
    }
  }
  '/api/v1/schemas': {
    post: {
      requestBody: {
        content: {
          'application/json': components['schemas']['SchemaCreateRequest']
        }
      }
      responses: {
        201: {
          content: {
            'application/json': components['schemas']['SchemaCreateResponse']
          }
        }
      }
    }
  }
  '/api/v1/feedback/inbox': {
    get: {
      parameters: {
        query: {
          status?: string
          pipeline_id?: string
          agent_id?: string
          date_from?: string
          date_to?: string
          page?: number
          page_size?: number
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['FeedbackInboxResponse']
          }
        }
      }
    }
  }
  '/api/v1/feedback/inbox/{record_id}': {
    get: {
      parameters: {
        path: { record_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['FeedbackRecordDetail']
          }
        }
      }
    }
  }
  '/api/v1/feedback/inbox/{record_id}/review': {
    post: {
      parameters: {
        path: { record_id: string }
      }
      requestBody: {
        content: {
          'application/json': components['schemas']['FeedbackReviewRequest']
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['FeedbackRecordDetail']
          }
        }
      }
    }
  }
  '/api/v1/feedback/proposals': {
    get: {
      parameters: {
        query: {
          status?: string
          record_id?: string
          page?: number
          page_size?: number
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['FeedbackProposalListResponse']
          }
        }
      }
    }
  }
  '/api/v1/pipelines': {
    get: {
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['PipelineListResponse']
          }
        }
      }
    }
  }
  '/api/v1/variant-groups': {
    get: {
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['VariantGroupResponse'][]
          }
        }
      }
    }
    post: {
      requestBody: {
        content: {
          'application/json': components['schemas']['CreateVariantGroupRequest']
        }
      }
      responses: {
        201: {
          content: {
            'application/json': components['schemas']['VariantGroupResponse']
          }
        }
      }
    }
  }
  '/api/v1/variant-groups/{group_id}': {
    get: {
      parameters: {
        path: { group_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['VariantGroupResponse']
          }
        }
      }
    }
    put: {
      parameters: {
        path: { group_id: string }
      }
      requestBody: {
        content: {
          'application/json': components['schemas']['CreateVariantGroupRequest']
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['VariantGroupResponse']
          }
        }
      }
    }
    delete: {
      parameters: {
        path: { group_id: string }
      }
      responses: {
        204: { description: 'No content' }
      }
    }
  }
  '/api/v1/variant-groups/{group_id}/run': {
    post: {
      parameters: {
        path: { group_id: string }
      }
      requestBody: {
        content: {
          'application/json': components['schemas']['RunVariantRequest']
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['RunVariantResponse']
          }
        }
      }
    }
  }
  '/api/v1/model-backends': {
    get: {
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['ModelBackendListResponse']
          }
        }
      }
    }
  }
  '/api/v1/pipelines/{pipeline_id}/snapshots': {
    get: {
      parameters: {
        path: { pipeline_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['SnapshotListResponse']
          }
        }
      }
    }
  }
  '/api/v1/runs/{run_id}': {
    get: {
      parameters: {
        path: { run_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['RunResponse']
          }
        }
      }
    }
  }
  '/api/v1/runs/{run_id}/io': {
    get: {
      parameters: {
        path: { run_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['RunIOResponse']
          }
        }
      }
    }
  }
  '/api/v1/runs/{run_id}/evals': {
    get: {
      parameters: {
        path: { run_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['RunEvalListResponse']
          }
        }
      }
    }
  }
  '/api/v1/settings/observability': {
    get: {
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['OtelSettingsResponse']
          }
        }
      }
    }
    put: {
      requestBody: {
        content: {
          'application/json': components['schemas']['OtelSettingsUpdate']
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['OtelSettingsResponse']
          }
        }
      }
    }
  }
  '/api/v1/settings/observability/test': {
    post: {
      requestBody: {
        content: {
          'application/json': components['schemas']['TestOtelConfig']
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['TestSpanResult']
          }
        }
      }
    }
  }
}

export interface components {
  schemas: {
    DashboardSummaryResponse: {
      total_runs: number
      active_pipelines: number
      run_counts_by_status: {
        running: number
        awaiting_human: number
        failed: number
        idle: number
      }
    }
    AdminTeamItem: {
      id: string
      name: string
      description: string | null
      created_by: string
      member_count: number
      created_at: string
    }
    AdminTeamListResponse: {
      items: components['schemas']['AdminTeamItem'][]
      total: number
      page: number
      page_size: number
    }
    AdminCreateTeamRequest: {
      name: string
      description?: string | null
    }
    AdminCreateTeamResponse: {
      id: string
      name: string
      description: string | null
      created_by: string
      created_at: string
    }
    AdminUpdateTeamRequest: {
      name?: string | null
      description?: string | null
    }
    MembershipResponse: {
      id: string
      team_id: string
      user_id: string
      role: string
      created_at: string
    }
    MembershipListResponse: {
      items: components['schemas']['MembershipResponse'][]
      total: number
      page: number
      page_size: number
    }
    AddMemberRequest: {
      user_id: string
      role: string
    }
    OtelSettingsResponse: {
      otlp_endpoint: string
      otlp_headers: { [key: string]: string }
      export_interval_seconds: number
      langsmith_enabled: boolean
      has_langsmith_api_key: boolean
      effective_otlp_endpoint: string
      env_override_active: boolean
    }
    OtelSettingsUpdate: {
      otlp_endpoint?: string | null
      otlp_headers?: { [key: string]: string } | null
      export_interval_seconds?: number | null
      langsmith_enabled?: boolean | null
      langsmith_api_key?: string | null
    }
    TestOtelConfig: {
      otlp_endpoint: string
      otlp_headers?: { [key: string]: string }
    }
    TestSpanResult: {
      success: boolean
      message: string
    }
    AdminUserListItem: {
      id: string
      email: string
      display_name: string
      org_role: string
      is_active: boolean
      auth_provider: string
      created_at: string
      last_login: string | null
    }
    AdminUserListResponse: {
      items: components['schemas']['AdminUserListItem'][]
      total: number
      page: number
      page_size: number
    }
    SsoProviderResponse: {
      id: string
      provider_type: string
      name: string
      client_id: string | null
      discovery_url: string | null
      metadata_url: string | null
      metadata_xml: string | null
      entity_id: string | null
      scopes: string[] | null
      enabled: boolean
      auto_provision: boolean
      default_role: string
      created_at: string
      updated_at: string
    }
    SsoProviderCreate: {
      provider_type: string
      name: string
      client_id?: string | null
      client_secret?: string | null
      discovery_url?: string | null
      metadata_url?: string | null
      metadata_xml?: string | null
      entity_id?: string | null
      scopes?: string[] | null
      enabled?: boolean
      auto_provision?: boolean
      default_role?: string
    }
    SsoProviderUpdate: {
      name?: string | null
      client_id?: string | null
      client_secret?: string | null
      discovery_url?: string | null
      metadata_url?: string | null
      metadata_xml?: string | null
      entity_id?: string | null
      scopes?: string[] | null
      enabled?: boolean | null
      auto_provision?: boolean | null
      default_role?: string | null
    }
    SsoProviderTestResult: {
      success: boolean
      message: string
      provider_info: Record<string, unknown> | null
    }
    AuditEventResponse: {
      id: string
      event_type: string
      actor_user_id: string | null
      resource_type: string | null
      resource_id: string | null
      payload_json: { [key: string]: unknown }
      request_id: string | null
      previous_hash: string | null
      created_at: string | null
    }
    AuditLogListResponse: {
      items: components['schemas']['AuditEventResponse'][]
      total: number
      next_cursor: string | null
      prev_cursor: string | null
      limit: number
    }
    AuditExportResponse: {
      items: components['schemas']['AuditEventResponse'][]
      total: number
      page: number
      page_size: number
    }
    ChangelogEntry: {
      version: string
      date: string
      summary: string
      changes: string[]
      deprecations: string[] | null
      migration_url: string | null
    }
    ConnectorItem: {
      id: string
      name: string
      connector_type: string
      description: string | null
    }
    ConnectorListResponse: {
      items: components['schemas']['ConnectorItem'][]
    }
    SchemaInferRequest: {
      connector_instance_id: string
      resource_type: string
      sample_query?: string | null
    }
    SchemaFieldDefinition: {
      name: string
      type: string
      required: boolean
      description: string | null
    }
    SchemaInferResponse: {
      name: string
      description: string | null
      fields: components['schemas']['SchemaFieldDefinition'][]
    }
    SchemaCreateRequest: {
      name: string
      description?: string | null
      fields: components['schemas']['SchemaFieldDefinition'][]
    }
    SchemaCreateResponse: {
      id: string
      name: string
    }
    FeedbackRecordItem: {
      id: string
      pipeline_run_id: string
      pipeline_name: string
      agent_name: string | null
      feedback_type: string
      status: string
      handler_type: string | null
      rejection_reason: string | null
      summary: string | null
      created_at: string
      updated_at: string
    }
    FeedbackInboxResponse: {
      items: components['schemas']['FeedbackRecordItem'][]
      total: number
      page: number
      page_size: number
    }
    FeedbackRecordDetail: {
      id: string
      pipeline_run_id: string
      pipeline_name: string
      agent_name: string | null
      feedback_type: string
      status: string
      handler_type: string | null
      rejection_reason: string | null
      rejected_output: unknown
      correction_proposal: unknown | null
      annotation: string | null
      created_at: string
      updated_at: string
    }
    FeedbackReviewRequest: {
      annotation?: string | null
      status?: string | null
    }
    FeedbackProposalItem: {
      id: string
      record_id: string
      proposed_change: string
      status: string
      created_at: string
    }
    FeedbackProposalListResponse: {
      items: components['schemas']['FeedbackProposalItem'][]
      total: number
      page: number
      page_size: number
    }
    PipelineItem: {
      id: string
      name: string
      description: string | null
    }
    PipelineListResponse: {
      items: components['schemas']['PipelineItem'][]
      total: number
      page: number
      page_size: number
    }
    CreateVariantGroupRequest: {
      pipeline_id: string
      name: string
      description?: string | null
      variants: components['schemas']['VariantDef'][]
      selection_strategy?: string
      max_concurrent_runs?: number
      degraded_evals?: boolean
    }
    VariantGroupResponse: {
      id: string
      pipeline_id: string
      name: string
      description: string | null
      variants: components['schemas']['VariantDef'][]
      selection_strategy: string
      run_count: number
      max_concurrent_runs: number
      degraded_evals: boolean
      created_at: string
      updated_at: string
    }
    VariantDef: {
      snapshot_id: string
      name: string
      weight: number
      run_context_overrides: Record<string, unknown>
      eval_definition_ids: string[]
    }
    ModelBackendResponse: {
      id: string
      organisation_id: string
      name: string
      display_name: string
      provider: string
      model_id: string
      has_credentials: boolean
      default_params: Record<string, unknown>
      visibility: string
      created_by: string
      created_at: string
      updated_at: string
    }
    ModelBackendListResponse: {
      items: components['schemas']['ModelBackendResponse'][]
      total: number
      page: number
      page_size: number
    }
    SnapshotItem: {
      id: string
      pipeline_id: string
      snapshot_version: number
      tag: string | null
      notes: string | null
      created_at: string | null
      created_by: string | null
    }
    SnapshotListResponse: {
      items: components['schemas']['SnapshotItem'][]
      total: number
    }
    RunVariantRequest: {
      input_payload?: Record<string, unknown>
    }
    RunVariantResponse: {
      run_id: string
      variant_name: string
      merged_payload: Record<string, unknown>
    }
    RunResponse: {
      run_id: string
      status: string
      pipeline_id: string
      langgraph_thread_id: string
      error_detail: string | null
      error_code: string | null
      total_cost_usd: number | null
      token_consumption: Record<string, unknown> | null
      trace_id: string | null
      node_token_usage: Record<string, unknown> | null
    }
    RunIOResponse: {
      run_id: string
      status: string
      input_payload: Record<string, unknown> | null
      outputs_json: Record<string, unknown> | null
      fixture_map: Record<string, string> | null
    }
    RunEvalItem: {
      id: string
      run_id: string
      node_id: string | null
      eval_id: string
      passed: boolean
      score: number | null
      detail: string | null
      evaluated_at: string | null
    }
    RunEvalListResponse: {
      items: components['schemas']['RunEvalItem'][]
      total: number
      page: number
      page_size: number
    }
    RateLimitRuleResponse: {
      path_prefix: string
      max_requests: number
      window_s: number
    }
    RateLimitStatusResponse: {
      mode: string
      rules: components['schemas']['RateLimitRuleResponse'][]
    }
    RateLimitRuleUpdate: {
      path_prefix: string
      max_requests: number
      window_s: number
    }
    RateLimitUpdateRequest: {
      rules: components['schemas']['RateLimitRuleUpdate'][]
    }
  }
}
