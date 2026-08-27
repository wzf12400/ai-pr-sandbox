CREATE TABLE log_repository_route (
    id VARCHAR(36) PRIMARY KEY,
    container_name VARCHAR(128) NOT NULL,
    repository VARCHAR(255) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    version BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP(6) NOT NULL,
    updated_at TIMESTAMP(6) NOT NULL
);

CREATE UNIQUE INDEX uq_log_repository_route_container
    ON log_repository_route (container_name);

ALTER TABLE automation_job
    ADD COLUMN log_container_name VARCHAR(128);
