---
id: feat-core-determination
prd: 8.16
delivery-tasks: []
bdd: []
unit-tests: []
code:
  - backend/src/modulo/api/routes/determination.py
depends-on: [feat-connectors-hub]
status: gap
---

# SDLC Assessment and Pipeline Draft Generation

The determination endpoint scans a team's connected tools (GitHub, GitLab, Jira, Linear) to infer SDLC maturity and generate editable pipeline drafts.

## Behaviours

- [ ] GET /api/v1/determination — scan + infer SDLC maturity
- [ ] POST /api/v1/determination/draft — scan + generate editable pipeline draft
- [ ] Integration with GitHub/GitLab connectors for code activity analysis
- [ ] Integration with Jira/Linear connectors for issue tracking analysis
- [ ] Pipeline draft generation from inferred workflow
