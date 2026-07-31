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

    print(
        "INSERT INTO library_primitives "
        "(id, organisation_id, name, slug, description, primitive_type, "
        "author, version, visibility, source, tags, content_json, "
        "account_id, auto_update, category, created_at, updated_at) "
        "VALUES ("
        f"'{t.id}', "
        f"'{ORG_ID}', "
        f"'{name_esc}', "
        f"'{slug_esc}', "
        f"'{desc_esc}', "
        "'pipeline_template', "
        "'modulo', "
        "'1.0', "
        "'community', "
        "'modulo', "
        f"'{tags_json}'::json, "
        f"'{content_json}'::json, "
        "(SELECT id FROM accounts ORDER BY created_at LIMIT 1), "
        "false, "
        f"'{category_esc}', "
        "NOW(), NOW()"
        ") ON CONFLICT (id) DO NOTHING;"
    )
