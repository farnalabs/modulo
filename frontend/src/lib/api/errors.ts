import { api } from './client'
import type { components } from './client'
import { throwOnError } from './formatError'

export type ErrorGroupSummary = components['schemas']['ErrorGroupSummary']
export type ErrorGroupDetail = components['schemas']['ErrorGroupDetail']
export type ErrorEventDetail = components['schemas']['ErrorEventDetail']
export type ErrorListResponse = components['schemas']['ErrorListResponse']
export type ErrorEventListResponse = components['schemas']['ErrorEventListResponse']

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
