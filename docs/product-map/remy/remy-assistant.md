---
id: feat-remy-assistant
prd: 8.23, 8.27
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/remy/remy_sessions.feature
  - backend/tests/bdd/features/remy/remy_messages.feature
  - backend/tests/bdd/features/remy/remy_access_control.feature
unit-tests:
  - backend/tests/unit/api/test_remy.py
code:
  - backend/src/modulo/api/routes/remy.py
  - backend/src/modulo/db/models/remy_session.py
  - backend/src/modulo/db/models/remy_message.py
  - backend/src/modulo/db/models/remy_skill.py
  - backend/src/modulo/core/remy/
  - frontend/src/views/AdminRemyView.vue
  - frontend/src/views/UserRemySkillsView.vue
depends-on: [feat-model-backends-management]
status: partial
---

# Remy — In-App AI Assistant

Remy is a floating AI assistant overlay present on every authenticated page. It uses user-provided API keys to drive LLM-powered conversations and can execute UI commands via SSE stream.

## Behaviours

### Sessions

- [x] CRUD for chat sessions (list, create, get, rename, delete)
- [x] Sessions scoped per org and user
- [x] Session numbering (per-user incrementing session_number)
- [x] Provider/model stored per session

### Messages

- [x] Append messages to sessions
- [x] List messages with pagination
- [x] Message roles: user, assistant, system

### SSE Streaming

- [x] POST /api/v1/remy/sessions/{id}/stream — SSE stream for LLM responses
- [x] UI command routing through SSE stream
- [x] Permission/UI-command response handling

### Remy Skills

- [x] Org and user-scoped skills with triggers and body
- [x] Skill loader for runtime loading

### Admin Configuration

- [x] AdminRemyView for configuring Remy settings
- [x] UserRemySkillsView for managing personal skills
