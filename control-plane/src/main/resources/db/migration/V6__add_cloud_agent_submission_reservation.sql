ALTER TABLE automation_job
    ADD COLUMN agent_submission_key VARCHAR(64);

ALTER TABLE automation_job
    ADD COLUMN agent_issue_sha256 VARCHAR(64);

ALTER TABLE automation_job
    ADD COLUMN agent_policy_sha256 VARCHAR(64);
