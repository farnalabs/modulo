---
id: feat-core-tier-catalog
prd: 6
delivery-tasks: []
bdd: []
unit-tests: []
code:
  - backend/src/modulo/api/routes/admin_tiers.py
  - backend/src/modulo/db/models/tier_catalog.py
  - backend/src/modulo/db/crud/tier_catalog.py
depends-on: []
status: partial
---

# Tier Catalog

Plan tier definitions and feature flag catalog governing which features are available at each tier.

## Behaviours

### Models

- [x] `TierCatalog` — tier_id, label, rank, requires_license
- [x] `FeatureFlagCatalog` — name, tier_id, depends_on, is_active

### API

- [x] GET /api/v1/admin/tiers returns all plan tiers with labels
- [x] CRUD tier listing via admin_tiers route
