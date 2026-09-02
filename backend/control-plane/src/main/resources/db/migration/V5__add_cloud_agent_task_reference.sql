ALTER TABLE automation_job
    ADD COLUMN agent_task_id VARCHAR(160);

ALTER TABLE automation_job
    ADD COLUMN agent_task_url VARCHAR(512);
