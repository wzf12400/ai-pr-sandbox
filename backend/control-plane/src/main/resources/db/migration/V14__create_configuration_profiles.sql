CREATE TABLE configuration_profiles (
    profile_key VARCHAR(64) PRIMARY KEY,
    payload_json JSON NOT NULL,
    initialized BOOLEAN NOT NULL DEFAULT FALSE,
    source VARCHAR(64) NOT NULL DEFAULT 'SYSTEM',
    version BIGINT NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL
);

INSERT INTO configuration_profiles (
    profile_key, payload_json, initialized, source, version, created_at, updated_at
) VALUES (
    'CONSOLE_SETTINGS', '{}', FALSE, 'SYSTEM', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
);
