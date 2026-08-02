export interface ChatSession {
  id: string
  user_id: string
  name: string | null
  session_number: number | null
  provider: string
  model: string
  context_window_tokens: number
  system_prompt_hash: string | null
  message_count: number
  created_at: string
  updated_at: string
}

export interface ToolResult {
  tool_call_id: string
  tool_name: string
  success: boolean
  result?: unknown
  error?: string
}

export interface ChatMessage {
  id: string
  session_id: string
  role: 'user' | 'assistant' | 'tool_use' | 'tool_result' | 'summary'
  content: string | null
  tool_calls_json: Record<string, unknown> | null
  tool_results_json: ToolResult | null
  token_count: number | null
  parent_id: string | null
  created_at: string
}

export interface PageContext {
  route: string
  params: Record<string, string>
  entities: string[]
}

export interface SkillBase {
  id: string
  name: string
  description: string | null
  triggers: string[] | null
  body: string
}

export interface SkillItem extends SkillBase {
  active: boolean
  created_at: string
  updated_at: string
}

export interface UserSkill extends SkillBase {
  active?: boolean
  created_at: string
  updated_at: string
}

export type ContextSourceMode = 'always_on' | 'tool' | 'off'

export interface ContextSourceItem {
  key: string
  name: string
  description: string
  source_mode: ContextSourceMode
  is_overridden: boolean
}

export interface ContextSourceUpdate {
  source_mode: ContextSourceMode
}
