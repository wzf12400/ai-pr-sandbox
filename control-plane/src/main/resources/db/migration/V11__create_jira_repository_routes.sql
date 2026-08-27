CREATE TABLE jira_repository_route (
    id VARCHAR(36) PRIMARY KEY,
    project_key VARCHAR(20) NOT NULL,
    project_name VARCHAR(255) NOT NULL,
    match_type VARCHAR(32) NOT NULL,
    match_value VARCHAR(255) NOT NULL,
    repository VARCHAR(255) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    priority INT NOT NULL DEFAULT 100,
    version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL
);

CREATE UNIQUE INDEX uq_jira_repository_route_match
    ON jira_repository_route (project_key, match_type, match_value, repository);

CREATE INDEX idx_jira_repository_route_lookup
    ON jira_repository_route (project_key, enabled, priority);

INSERT INTO jira_repository_route (
    id,
    project_key,
    project_name,
    match_type,
    match_value,
    repository,
    enabled,
    priority,
    version,
    created_at,
    updated_at
) VALUES
    (
        'df97d20f-4897-4b6d-aa70-83f4fa1a80d7',
        'AI',
        '创新项目—ai陪伴',
        'PROJECT',
        '*',
        'KikaTech/frontend-aicompanion',
        TRUE,
        100,
        0,
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    ),
    (
        'a34fb6d1-dc60-446e-a94c-031d4c16bd71',
        'AI',
        '创新项目—ai陪伴',
        'PROJECT',
        '*',
        'KikaTech/backend-aicompanion',
        TRUE,
        100,
        0,
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    );
