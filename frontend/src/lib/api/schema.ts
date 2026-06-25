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
  }
}
