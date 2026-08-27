CREATE UNIQUE INDEX uq_automation_job_source_identity
    ON automation_job (source_type, source_reference);
