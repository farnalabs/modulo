ALTER TABLE pipeline_snapshots ADD COLUMN IF NOT EXISTS default_autonomy_level VARCHAR(30);
ALTER TABLE pipeline_snapshots ADD COLUMN IF NOT EXISTS environment_profile_id UUID;
ALTER TABLE pipeline_snapshots ADD COLUMN IF NOT EXISTS tag VARCHAR(100);
ALTER TABLE pipeline_snapshots ADD COLUMN IF NOT EXISTS notes VARCHAR(2000);
ALTER TABLE pipeline_snapshots ADD COLUMN IF NOT EXISTS config_json JSON DEFAULT '{}';
ALTER TABLE pipeline_snapshots ADD COLUMN IF NOT EXISTS run_context_defaults JSON DEFAULT '{}';
