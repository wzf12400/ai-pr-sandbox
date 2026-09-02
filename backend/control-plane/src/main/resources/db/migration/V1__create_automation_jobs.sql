CREATE TABLE automation_job (
    id VARCHAR(36) PRIMARY KEY,
    source_type VARCHAR(32) NOT NULL,
    input_summary TEXT NOT NULL,
    normalized_requirement TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    matched_repository VARCHAR(255),
    routing_basis VARCHAR(512),
    routing_confidence INTEGER,
    routing_candidates VARCHAR(2000),
    issue_number BIGINT,
    issue_url VARCHAR(512),
    pr_number BIGINT,
    pr_url VARCHAR(512),
    test_summary TEXT,
    blocked_reason VARCHAR(1000),
    retry_count INTEGER NOT NULL DEFAULT 0,
    submitted_by VARCHAR(128) NOT NULL,
    policy_id VARCHAR(128) NOT NULL,
    version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL
);

CREATE INDEX idx_automation_job_status_created
    ON automation_job (status, created_at);

CREATE TABLE job_event (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    from_status VARCHAR(32),
    to_status VARCHAR(32) NOT NULL,
    actor_type VARCHAR(32) NOT NULL,
    detail VARCHAR(1000),
    created_at TIMESTAMP(6) NOT NULL,
    CONSTRAINT fk_job_event_job
        FOREIGN KEY (job_id) REFERENCES automation_job (id)
);

CREATE INDEX idx_job_event_job_created
    ON job_event (job_id, created_at);
