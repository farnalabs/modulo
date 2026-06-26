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
  }
}
