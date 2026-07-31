"""Generate SQL INSERT statements for seeding pipeline templates."""

import json

from modulo.core.library_service import _MODULO_PRIMITIVES

ORG_ID = "6a6d7112-2058-4d65-ac75-465a12a94563"

templates = [p for p in _MODULO_PRIMITIVES if p.primitive_type == "pipeline_template"]

for t in templates:
    content = dict(t.content_json)
    content_json = json.dumps(content).replace("'", "''")
    tags_json = json.dumps(list(t.tags) if t.tags else []).replace("'", "''")
    name_esc = t.name.replace("'", "''")
    desc_esc = (t.description or "").replace("'", "''")
    slug_esc = t.slug.replace("'", "''")
    category_esc = content.get("category", "").replace("'", "''")

