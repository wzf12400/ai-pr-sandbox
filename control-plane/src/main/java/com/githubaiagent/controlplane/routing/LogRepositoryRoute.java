package com.githubaiagent.controlplane.routing;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;

import java.time.Instant;

@Entity
@Table(name = "log_repository_route")
public class LogRepositoryRoute {

    @Id
    @Column(length = 36, nullable = false)
    private String id;

    @Column(name = "selector_field", length = 64, nullable = false)
    private String selectorField;

    @Column(name = "selector_value", length = 128, nullable = false)
    private String selectorValue;

    @Column(length = 255, nullable = false)
    private String repository;

    @Column(nullable = false)
    private boolean enabled;

    @Version
    @Column(nullable = false)
    private long version;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected LogRepositoryRoute() {
    }

    public LogRepositoryRoute(
            String id,
            String selectorField,
            String selectorValue,
            String repository,
            boolean enabled,
            Instant now
    ) {
        this.id = id;
        this.selectorField = selectorField;
        this.selectorValue = selectorValue;
        this.repository = repository;
        this.enabled = enabled;
        this.createdAt = now;
        this.updatedAt = now;
    }

    public void update(
            String selectorField,
            String selectorValue,
            String repository,
            boolean enabled,
            Instant now
    ) {
        this.selectorField = selectorField;
        this.selectorValue = selectorValue;
        this.repository = repository;
        this.enabled = enabled;
        this.updatedAt = now;
    }

    public String getId() {
        return id;
    }

    public String getSelectorField() {
        return selectorField;
    }

    public String getSelectorValue() {
        return selectorValue;
    }

    public String getRepository() {
        return repository;
    }

    public boolean isEnabled() {
        return enabled;
    }

    public long getVersion() {
        return version;
    }

    public Instant getCreatedAt() {
        return createdAt;
    }

    public Instant getUpdatedAt() {
        return updatedAt;
    }
}
