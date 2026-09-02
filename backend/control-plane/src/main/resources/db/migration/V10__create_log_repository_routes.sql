CREATE TABLE log_repository_route (
    id VARCHAR(36) PRIMARY KEY,
    selector_field VARCHAR(64) NOT NULL,
    selector_value VARCHAR(128) NOT NULL,
    repository VARCHAR(255) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL
);

CREATE UNIQUE INDEX uq_log_repository_route_selector
    ON log_repository_route (selector_field, selector_value);

ALTER TABLE automation_job
    ADD COLUMN log_route_id VARCHAR(36);

INSERT INTO log_repository_route (
    id, selector_field, selector_value, repository, enabled, version, created_at, updated_at
) VALUES
    (
        '0d4cf4e2-084b-4f45-84a8-e48783b275c0',
        'kubernetes.container_name.keyword',
        'backend-aicompanion',
        'KikaTech/backend-aicompanion',
        TRUE,
        0,
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    ),
    (
        'c650a533-fb4f-4fce-92e3-43fb42685f9f',
        'kubernetes.container_name.keyword',
        'frontend-aicompanion',
        'KikaTech/frontend-aicompanion',
        TRUE,
        0,
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    ),
    (
        '3e052a57-a694-4200-b99d-d89620b01ef9',
        'kubernetes.container_name.keyword',
        'kika-global-studio',
        'KikaTech/kika-global-studio',
        TRUE,
        0,
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    ),
    (
        'e39e45a0-6811-451b-a59a-cde52ce9ca62',
        'kubernetes.container_name.keyword',
        'kika-global-studio-front',
        'KikaTech/kika-global-studio-front',
        TRUE,
        0,
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    );
