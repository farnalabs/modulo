"""Create the Modulo SDLC Pipeline directly on the production DB."""
import os, uuid, json, asyncio
os.environ.setdefault('DATABASE_URL', os.environ.get('DATABASE_URL', ''))
import asyncpg

async def main():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])

    try:
        row = await conn.fetchrow("SELECT id FROM pipelines WHERE name = 'Modulo SDLC Pipeline'")
        if row:
            print(f'ALREADY EXISTS: {row["id"]}')
            return

        tables = await conn.fetch("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('accounts','users')")
        table_names = [t['table_name'] for t in tables]
        user_table = 'accounts' if 'accounts' in table_names else 'users'

        org = await conn.fetchrow("SELECT id FROM organisations ORDER BY created_at LIMIT 1")
        user = await conn.fetchrow(f"SELECT id FROM {user_table} WHERE email = 'admin@modulo.run' LIMIT 1")
        if not user:
            user = await conn.fetchrow(f"SELECT id FROM {user_table} LIMIT 1")

        cols = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name='pipelines' AND column_name IN ('account_id','created_by')")
        col_names = [c['column_name'] for c in cols]
        user_fk_col = 'account_id' if 'account_id' in col_names else 'created_by'
        print(f'user_table={user_table}, user_fk_col={user_fk_col}')

        pid = uuid.uuid4()
        nodes = json.dumps([
            {"id":"00000000-0000-0000-0000-000000000001","node_type":"agent","agent_id":None,"position":{"x":100,"y":0},"label":"Task Picker / Scheduler","description":"Reads delivery plan queue, picks next ready task, creates worktree branch"},
            {"id":"00000000-0000-0000-0000-000000000002","node_type":"agent","agent_id":None,"position":{"x":100,"y":150},"label":"Worker Executor","description":"Spawns isolated Worker sub-agent, implements code, runs tests, commits to worktree"},
            {"id":"00000000-0000-0000-0000-000000000003","node_type":"agent","agent_id":None,"position":{"x":100,"y":300},"label":"QA Engine (7 lenses)","description":"7-lens multi-agent scan: behaviour, edge cases, error paths, cross-module, gaps, security, performance"},
            {"id":"00000000-0000-0000-0000-000000000004","node_type":"manual","agent_id":None,"position":{"x":100,"y":450},"label":"Merge Gate","output_schema_id":"00000000-0000-0000-0000-000000000000","description":"Conflict reconciliation, gates (lint, tests, import smoke), merge to main, cleanup"},
            {"id":"00000000-0000-0000-0000-000000000005","node_type":"agent","agent_id":None,"position":{"x":100,"y":600},"label":"Publish Gate","description":"Scheduled (8hr): verify main clean, run tests, push to remote"},
            {"id":"00000000-0000-0000-0000-000000000006","node_type":"manual","agent_id":None,"position":{"x":100,"y":750},"label":"Deploy Pipeline","output_schema_id":"00000000-0000-0000-0000-000000000000","description":"Manual: tests, typecheck, build frontend, fly deploy, verify health"}
        ])

        await conn.execute(f'''
            INSERT INTO pipelines (id, organisation_id, name, description, {user_fk_col}, visibility,
                max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds,
                run_context_defaults, graph_nodes_json, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::json, $11::json, NOW(), NOW())
        ''', pid, org['id'], 'Modulo SDLC Pipeline',
           'Logical SDLC - task picker, worker exec, QA, merge, publish, deploy',
           user['id'], 'org', 1, 300, 300, '{}', nodes)

        org_id = org['id']
        edges = [
            ('00000000-0000-0000-0000-000000000010', '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002', 'normal'),
            ('00000000-0000-0000-0000-000000000011', '00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000003', 'normal'),
            ('00000000-0000-0000-0000-000000000012', '00000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000002', 'reject'),
            ('00000000-0000-0000-0000-000000000013', '00000000-0000-0000-0000-000000000003', '00000000-0000-0000-0000-000000000004', 'normal'),
            ('00000000-0000-0000-0000-000000000014', '00000000-0000-0000-0000-000000000004', '00000000-0000-0000-0000-000000000005', 'normal'),
            ('00000000-0000-0000-0000-000000000015', '00000000-0000-0000-0000-000000000005', '00000000-0000-0000-0000-000000000006', 'normal'),
        ]
        for eid, src, tgt, etype in edges:
            await conn.execute(f'''
                INSERT INTO pipeline_edges (id, organisation_id, pipeline_id, source_node_id, target_node_id, edge_type)
                VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid, $6)
            ''', eid, org_id, pid, src, tgt, etype)

        print(f'CREATED: {pid}')

    finally:
        await conn.close()

asyncio.run(main())
