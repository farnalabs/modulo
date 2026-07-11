---
id: feat-remy-assistant
prd: 8.23, 8.27
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/remy/remy_sessions.feature
  - backend/tests/bdd/features/remy/remy_messages.feature
  - backend/tests/bdd/features/remy/remy_access_control.feature
  - backend/tests/bdd/features/remy/remy_admin_config.feature
  - backend/tests/bdd/features/remy/remy_skills.feature
  - backend/tests/bdd/features/remy/remy_ui_commands.feature
  - backend/tests/bdd/features/remy/remy_context_window.feature
  - backend/tests/bdd/features/remy/remy_context_sources.feature
unit-tests:
  - backend/tests/unit/remy/test_config_service.py
  - backend/tests/unit/remy/test_context_source_service.py
  - backend/tests/unit/remy/test_context_tools.py
  - backend/tests/unit/remy/test_documentation_indexer.py
  - backend/tests/unit/remy/test_manifest.py
  - backend/tests/unit/remy/test_skill_loader.py
  - backend/tests/unit/remy/test_ui_commands_api.py
  - backend/tests/unit/remy/test_ui_tools.py
  - backend/tests/unit/db/test_remy_models.py
code:
  - backend/src/modulo/api/routes/remy.py
  - backend/src/modulo/api/routes/admin_remy.py
  - backend/src/modulo/db/models/remy_session.py
  - backend/src/modulo/db/models/remy_message.py
  - backend/src/modulo/db/models/remy_skill.py
  - backend/src/modulo/core/remy/
  - backend/src/modulo/core/remy/skill_loader.py
  - backend/src/modulo/core/remy/config_service.py
  - backend/src/modulo/core/remy/context_source_service.py
  - backend/src/modulo/core/remy/redis_registry.py
  - frontend/src/views/AdminRemyView.vue
  - frontend/src/views/UserRemySkillsView.vue
  - frontend/src/components/remy/RemyPanel.vue
  - frontend/src/components/remy/RemyChat.vue
  - frontend/src/components/remy/RemySessionDrawer.vue
  - frontend/src/components/remy/RemySkillManager.vue
  - frontend/src/components/remy/RemySkillDialog.vue
  - frontend/src/composables/useRemyStore.ts
  - frontend/src/composables/useRemyStream.ts
  - frontend/src/composables/useRemyContext.ts
  - frontend/src/composables/useUiCommandExecutor.ts
  - frontend/src/types/remy.ts
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
