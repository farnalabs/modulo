import { api } from './client'
import type { components } from './client'
import { throwOnError } from './formatError'

export type ErrorGroupSummary = components['schemas']['ErrorGroupSummary']
export type ErrorGroupDetail = components['schemas']['ErrorGroupDetail']
export type ErrorEventDetail = components['schemas']['ErrorEventDetail']
export type ErrorListResponse = components['schemas']['ErrorListResponse']
export type ErrorEventListResponse = components['schemas']['ErrorEventListResponse']
export type SchedulerStarvationItem = components['schemas']['SchedulerStarvationItem']
export type SchedulerStarvationResponse = components['schemas']['SchedulerStarvationResponse']

export interface FetchErrorGroupsParams {
  status?: string
  level?: string
  source?: string
  environment?: string
  search?: string
  limit?: number
  offset?: number
}

export async function fetchErrorGroups(params: FetchErrorGroupsParams = {}): Promise<ErrorListResponse> {
  return throwOnError(await api.GET('/api/v1/errors', {
    params: { query: params as unknown as Record<string, unknown> },
  })) as ErrorListResponse
}

export async function fetchErrorGroup(id: string): Promise<ErrorGroupDetail> {
  return throwOnError(await api.GET('/api/v1/errors/{error_id}', {
    params: { path: { error_id: id } },
  })) as ErrorGroupDetail
}

export async function updateErrorGroup(id: string, body: { status?: string; assigned_to?: string }): Promise<ErrorGroupDetail> {
  return throwOnError(await api.PATCH('/api/v1/errors/{error_id}', {
    params: { path: { error_id: id } },
    body: body as unknown as Record<string, unknown>,
  })) as ErrorGroupDetail
}

export async function fetchErrorGroupEvents(id: string, params: { limit?: number; offset?: number } = {}): Promise<ErrorEventListResponse> {
  return throwOnError(await api.GET('/api/v1/errors/{error_id}/events', {
    params: { path: { error_id: id }, query: params as unknown as Record<string, unknown> },
  })) as ErrorEventListResponse
}

// FAR-1547 instance-scope reads: the SYSTEM_ORG_ID sentinel partition, gated
// server-side by require_system_permission("errors.resolve_instance"). The
// client mirrors the gate by only calling these when the is_system_admin
// claim is present; a forged ?scope=instance without the claim resolves to
// the tenant helpers above and the backend refuses any instance read 403.
export async function fetchInstanceErrorGroups(params: FetchErrorGroupsParams = {}): Promise<ErrorListResponse> {
  return throwOnError(await api.GET('/api/v1/errors/instance', {
    params: { query: params as unknown as Record<string, unknown> },
  })) as ErrorListResponse
}

export async function fetchInstanceErrorGroup(id: string): Promise<ErrorGroupDetail> {
  return throwOnError(await api.GET('/api/v1/errors/instance/{error_id}', {
    params: { path: { error_id: id } },
  })) as ErrorGroupDetail
}

export async function fetchInstanceErrorGroupEvents(id: string, params: { limit?: number; offset?: number } = {}): Promise<ErrorEventListResponse> {
  return throwOnError(await api.GET('/api/v1/errors/instance/{error_id}/events', {
    params: { path: { error_id: id }, query: params as unknown as Record<string, unknown> },
  })) as ErrorEventListResponse
}

export async function fetchSchedulerStarvation(): Promise<SchedulerStarvationResponse> {
  return throwOnError(await api.GET('/api/v1/errors/scheduler-starvation')) as SchedulerStarvationResponse
}
