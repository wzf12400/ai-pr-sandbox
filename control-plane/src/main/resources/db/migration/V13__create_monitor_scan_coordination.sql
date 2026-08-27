CREATE TABLE monitor_scan_coordination (
    scanner_key VARCHAR(16) PRIMARY KEY,
    lease_owner VARCHAR(128),
    lease_token VARCHAR(36),
    lease_expires_at DATETIME(6),
    checkpoint_json MEDIUMTEXT,
    last_started_at DATETIME(6),
    last_completed_at DATETIME(6),
    last_error VARCHAR(500),
    version BIGINT NOT NULL DEFAULT 0,
    updated_at DATETIME(6) NOT NULL
);

INSERT INTO monitor_scan_coordination (
    scanner_key, checkpoint_json, version, updated_at
) VALUES
    ('JIRA', '{}', 0, CURRENT_TIMESTAMP),
    ('LOG', '{}', 0, CURRENT_TIMESTAMP);
