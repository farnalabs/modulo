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
  }
}
