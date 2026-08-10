ALTER TABLE automation_job
    ADD COLUMN issue_profile VARCHAR(32) NOT NULL DEFAULT 'NATURAL_LANGUAGE';
ALTER TABLE automation_job ADD COLUMN source_reference VARCHAR(128);
ALTER TABLE automation_job ADD COLUMN first_seen_at TIMESTAMP(6);
ALTER TABLE automation_job ADD COLUMN last_seen_at TIMESTAMP(6);
ALTER TABLE automation_job ADD COLUMN current_scan_event_count INTEGER;
ALTER TABLE automation_job ADD COLUMN historical_event_count INTEGER;
ALTER TABLE automation_job ADD COLUMN incident_group_count INTEGER;
ALTER TABLE automation_job ADD COLUMN affected_endpoints VARCHAR(4000);
ALTER TABLE automation_job ADD COLUMN affected_user_count_min INTEGER;
ALTER TABLE automation_job ADD COLUMN affected_user_count_max INTEGER;
ALTER TABLE automation_job ADD COLUMN user_identifier_event_count INTEGER;
ALTER TABLE automation_job ADD COLUMN historical_count_complete BOOLEAN;
ALTER TABLE automation_job ADD COLUMN aggregation_basis VARCHAR(1000);
