import os, re
env = open('/app/.env').read()
match = re.search(r'DATABASE_URL=(.+)', env)
url = match.group(1).strip().replace('+asyncpg', '+psycopg')
import psycopg
conn = psycopg.connect(url)
conn.execute('CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ix_schema_versions_schema_version ON schema_versions(schema_id, version)')
conn.execute('ALTER TABLE schema_versions ADD CONSTRAINT uq_schema_versions_schema_version UNIQUE USING INDEX ix_schema_versions_schema_version')
conn.commit()
conn.close()
print('Unique constraint added')
