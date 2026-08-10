ALTER TABLE automation_job
    ADD COLUMN execution_mode VARCHAR(32) NOT NULL DEFAULT 'MOCK' AFTER source_type;
