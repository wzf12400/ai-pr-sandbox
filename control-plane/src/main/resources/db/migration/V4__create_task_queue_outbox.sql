CREATE TABLE task_queue_outbox (
    task_id VARCHAR(36) PRIMARY KEY,
    generation BIGINT NOT NULL DEFAULT 1,
    published_generation BIGINT NOT NULL DEFAULT 0,
    publish_attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error VARCHAR(256),
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL,
    published_at TIMESTAMP(6),
    version BIGINT NOT NULL DEFAULT 0,
    CONSTRAINT fk_task_queue_outbox_job
        FOREIGN KEY (task_id) REFERENCES automation_job (id) ON DELETE CASCADE
);

CREATE INDEX idx_task_queue_outbox_pending
    ON task_queue_outbox (published_generation, generation, updated_at);
