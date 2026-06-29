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
  '/api/v1/admin/runtime-config': {
    get: {
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['RuntimeConfigResponse']
          }
        }
      }
    }
    put: {
      requestBody: {
        content: {
          'application/json': components['schemas']['RuntimeConfigUpdateRequest']
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['RuntimeConfigResponse']
          }
        }
      }
    }
  }
  '/api/v1/admin/runtime-config/reload': {
    post: {
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['RuntimeConfigResponse']
          }
        }
      }
    }
  }
  '/api/v1/admin/notifications/deliveries': {
    get: {
      parameters: {
        query?: {
          cursor?: string
          limit?: number
          status?: string
          from?: string
          to?: string
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['DeliveryLogResponse']
          }
        }
      }
    }
  }
  '/api/v1/admin/trigger-events': {
    get: {
      parameters: {
        query: {
          trigger_type?: string
          validation_result?: string
          cursor?: string
          limit?: number
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['TriggerEventListResponse']
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
  '/api/v1/triggers': {
    get: {
      parameters: {
        query: {
          pipeline_id?: string
          trigger_type?: string
          page?: number
          page_size?: number
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['TriggerListResponse']
          }
        }
      }
    }
  }
  '/api/v1/triggers/{trigger_id}': {
    put: {
      parameters: {
        path: { trigger_id: string }
      }
      requestBody: {
        content: {
          'application/json': components['schemas']['TriggerUpdate']
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['TriggerItem']
          }
        }
      }
    }
    delete: {
      parameters: {
        path: { trigger_id: string }
      }
      responses: {
        204: { description: 'No content' }
      }
    }
  }
  '/api/v1/triggers/{trigger_id}/toggle': {
    post: {
      parameters: {
        path: { trigger_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': {
              id: string
              active: boolean
            }
          }
        }
      }
    }
  }
  '/api/v1/pipelines/{pipeline_id}/triggers': {
    post: {
      parameters: {
        path: { pipeline_id: string }
      }
      requestBody: {
        content: {
          'application/json': components['schemas']['TriggerCreate']
        }
      }
      responses: {
        201: {
          content: {
            'application/json': components['schemas']['TriggerItem']
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
    post: {
      requestBody: {
        content: {
          'application/json': components['schemas']['ConnectorCreate']
        }
      }
      responses: {
        201: {
          content: {
            'application/json': components['schemas']['ConnectorResponse']
          }
        }
      }
    }
  }
  '/api/v1/connectors/{connector_id}': {
    get: {
      parameters: {
        path: { connector_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['ConnectorResponse']
          }
        }
      }
    }
    put: {
      parameters: {
        path: { connector_id: string }
      }
      requestBody: {
        content: {
          'application/json': components['schemas']['ConnectorUpdate']
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['ConnectorResponse']
          }
        }
      }
    }
    delete: {
      parameters: {
        path: { connector_id: string }
      }
      responses: {
        204: { description: 'No content' }
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
    get: {
      parameters: {
        query: {
          page?: number
          page_size?: number
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['SchemaListResponse']
          }
        }
      }
    }
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
  '/api/v1/schemas/{schema_id}': {
    get: {
      parameters: {
        path: { schema_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['SchemaItem']
          }
        }
      }
    }
  }
  '/api/v1/schemas/{schema_id}/deprecate': {
    patch: {
      parameters: {
        path: { schema_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['SchemaItem']
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
    post: {
      requestBody: {
        content: {
          'application/json': components['schemas']['ModelBackendCreate']
        }
      }
      responses: {
        201: {
          content: {
            'application/json': components['schemas']['ModelBackendResponse']
          }
        }
      }
    }
  }
  '/api/v1/model-backends/{backend_id}': {
    get: {
      parameters: {
        path: { backend_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['ModelBackendResponse']
          }
        }
      }
    }
    patch: {
      parameters: {
        path: { backend_id: string }
      }
      requestBody: {
        content: {
          'application/json': components['schemas']['ModelBackendUpdate']
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['ModelBackendResponse']
          }
        }
      }
    }
    delete: {
      parameters: {
        path: { backend_id: string }
      }
      responses: {
        204: { description: 'No content' }
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
  '/api/v1/notifications': {
    get: {
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['NotificationEndpointResponse'][]
          }
        }
      }
    }
    post: {
      requestBody: {
        content: {
          'application/json': components['schemas']['NotificationEndpointCreate']
        }
      }
      responses: {
        201: {
          content: {
            'application/json': components['schemas']['NotificationEndpointResponse']
          }
        }
      }
    }
  }
  '/api/v1/notifications/{endpoint_id}': {
    get: {
      parameters: {
        path: { endpoint_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['NotificationEndpointResponse']
          }
        }
      }
    }
    put: {
      parameters: {
        path: { endpoint_id: string }
      }
      requestBody: {
        content: {
          'application/json': components['schemas']['NotificationEndpointUpdate']
        }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['NotificationEndpointResponse']
          }
        }
      }
    }
    delete: {
      parameters: {
        path: { endpoint_id: string }
      }
      responses: {
        204: { description: 'No content' }
      }
    }
  }
  '/api/v1/admin/notifications/{webhook_id}/test': {
    post: {
      parameters: {
        path: { webhook_id: string }
      }
      responses: {
        200: {
          content: {
            'application/json': components['schemas']['TestResult']
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
      teams: Array<{
        id: string
        name: string
        total_runs: number
        active_pipelines: number
        run_counts_by_status: {
          running: number
          awaiting_human: number
          failed: number
          idle: number
        }
        eval_pass_rate?: {
          total_evals: number
          passed_evals: number
          pass_rate: number
        }
      }>
      eval_pass_rate: {
        overall_pass_rate: number
        total_evals: number
        passed_evals: number
        per_pipeline: Record<string, {
          total_evals: number
          passed_evals: number
          pass_rate: number
        }>
        per_team_pipeline: Record<string, Record<string, {
          total_evals: number
          passed_evals: number
          pass_rate: number
        }>>
      } | null
      trend: Array<{
        date: string
        run_count: number
        eval_pass_rate: number | null
        token_spend_usd: number
      }>
      recent_runs: Array<{
        id: string
        pipeline_name: string
        status: string
        created_at: string
        trigger_type: string
      }>
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
    ConnectorCreate: {
      name: string
      connector_type: string
      description?: string | null
      config_json?: string | null
    }
    ConnectorUpdate: {
      name?: string | null
      description?: string | null
      config_json?: string | null
    }
    ConnectorResponse: {
      id: string
      name: string
      connector_type: string
      description: string | null
      config_json: string | null
      enabled: boolean
      created_at: string | null
      updated_at: string | null
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
    SchemaItem: {
      id: string
      organisation_id: string
      name: string
      description: string | null
      abstract_name: string | null
      created_by: string
      created_at: string
      updated_at: string
      deprecated: boolean
      deprecated_at: string | null
    }
    SchemaListResponse: {
      items: components['schemas']['SchemaItem'][]
      total: number
      page: number
      page_size: number
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
      fallback_backend_ids: string[] | null
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
    ModelBackendCreate: {
      name: string
      display_name: string
      provider: string
      model_id: string
      api_key: string
      default_params?: Record<string, unknown>
      visibility?: string
      fallback_backend_ids?: string[] | null
    }
    ModelBackendUpdate: {
      name?: string | null
      display_name?: string | null
      model_id?: string | null
      api_key?: string | null
      default_params?: Record<string, unknown> | null
      visibility?: string | null
      fallback_backend_ids?: string[] | null
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
    RuntimeConfigEntry: {
      key: string
      current_value: string | null
      default_value: string | null
      env_value: string | null
      override_value: string | null
      provenance: string
      hot_reloadable: boolean
    }
    RuntimeConfigResponse: {
      items: components['schemas']['RuntimeConfigEntry'][]
      has_drift: boolean
    }
    RuntimeConfigUpdateRequest: {
      overrides?: { [key: string]: string }
      clear?: string[]
    }
    DeliveryLogEntry: {
      id: string
      event_type: string
      status: string
      attempt_count: number
      response_code: number | null
      last_error: string | null
      response_body: string | null
      endpoint_url: string | null
      created_at: string
    }
    DeliveryLogResponse: {
      items: components['schemas']['DeliveryLogEntry'][]
      next_cursor: string | null
      total: number
    }
    TriggerUpdate: {
      active?: boolean
      max_concurrent_runs?: number
      config_json?: Record<string, unknown>
      cron_expression?: string | null
      cron_timezone?: string | null
    }
    TriggerCreate: {
      trigger_type: string
      active?: boolean
      max_concurrent_runs?: number
      config_json?: Record<string, unknown>
      cron_expression?: string | null
      cron_timezone?: string | null
    }
    TriggerEventItem: {
      id: string
      trigger_id: string
      trigger_type: string
      validation_result: string
      received_at: string | null
      created_at: string | null
      run_id: string | null
      error_detail: string | null
    }
    TriggerEventListResponse: {
      items: components['schemas']['TriggerEventItem'][]
      next_cursor: string | null
      prev_cursor: string | null
      total: number
    }
    TriggerItem: {
      id: string
      pipeline_id: string
      trigger_type: string
      active: boolean
      max_concurrent_runs: number
      config_json: Record<string, unknown>
      cron_expression: string | null
      cron_timezone: string | null
      last_fired_at: string | null
      next_fire_at: string | null
      created_by: string
      created_at?: string | null
    }
    TriggerListResponse: {
      items: components['schemas']['TriggerItem'][]
      total: number
      page: number
      page_size: number
    }
    McpConfigResponse: {
      mcp_url: string
      config_snippet: string
    }
    ApiKeyItem: {
      id: string
      prefix: string
      name: string
      role: string
      is_active: boolean
      last_used_at: string | null
      created_at: string
    }
    ApiKeyCreatedResponse: {
      id: string
      key_value: string
      name: string
      role: string
    }
    CreateApiKeyRequest: {
      name: string
      role: string
    }
    UpdateApiKeyRequest: {
      name?: string
      is_active?: boolean
    }
    ApiKeyListResponse: {
      items: components['schemas']['ApiKeyItem'][]
    }
    OAuthClientItem: {
      id: string
      client_id: string
      name: string
      scopes: string[]
      redirect_uris: string[]
      created_at: string
    }
    OAuthClientListResponse: {
      items: components['schemas']['OAuthClientItem'][]
    }
    CreateOAuthClientRequest: {
      name: string
      redirect_uris: string[]
      scopes: string[]
    }
    NotificationEndpointResponse: {
      id: string
      url: string
      events: string[]
      description: string | null
      auto_disabled: boolean
      consecutive_dead_letter_count: number
      team_id: string | null
    }
    NotificationEndpointCreate: {
      url: string
      secret?: string | null
      events?: string[]
      description?: string | null
      team_id?: string | null
    }
    NotificationEndpointUpdate: {
      url?: string | null
      secret?: string | null
      events?: string[] | null
      description?: string | null
      team_id?: string | null
    }
    TestResult: {
      success: boolean
      status_code: number | null
      response_body: string | null
      error: string | null
    }
  }
}
