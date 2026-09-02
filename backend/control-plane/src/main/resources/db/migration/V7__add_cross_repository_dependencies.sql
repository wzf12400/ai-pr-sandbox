ALTER TABLE automation_job
    ADD COLUMN parent_task_id VARCHAR(36);

ALTER TABLE automation_job
    ADD COLUMN dependency_reason_code VARCHAR(64);

ALTER TABLE automation_job
    ADD COLUMN dependency_summary VARCHAR(1000);

CREATE INDEX idx_automation_job_parent_task
    ON automation_job (parent_task_id, created_at);

CREATE UNIQUE INDEX uq_automation_job_parent_repository
    ON automation_job (parent_task_id, matched_repository);
