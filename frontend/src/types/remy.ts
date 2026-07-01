export interface ChatSession {
  id: string
  name: string | null
  provider: string
  model: string
  context_window_tokens: number
  message_count: number
  created_at: string
  updated_at: string
}

export interface ChatMessage {
  id: string
  session_id: string
  role: string
  content: string | null
  token_count: number | null
  created_at: string
}

export interface PageContext {
  route: string
  params: Record<string, string>
  entities: string[]
}

export interface UserSkill {
  id: string
  name: string
  description: string
  triggers: string[]
  body: string
  created_at: string
  updated_at: string
}
