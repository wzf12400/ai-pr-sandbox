package com.githubaiagent.controlplane.routing;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;

import java.time.Instant;

@Entity
@Table(name = "jira_repository_route")
public class JiraRepositoryRoute {

    @Id
    @Column(length = 36, nullable = false)
    private String id;

    @Column(name = "project_key", length = 20, nullable = false)
    private String projectKey;

    @Column(name = "project_name", length = 255, nullable = false)
    private String projectName;

    @Column(name = "match_type", length = 32, nullable = false)
    private String matchType;

    @Column(name = "match_value", length = 255, nullable = false)
    private String matchValue;

    @Column(length = 255, nullable = false)
    private String repository;

    @Column(nullable = false)
    private boolean enabled;

    @Column(nullable = false)
    private int priority;

    @Version
    @Column(nullable = false)
    private long version;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected JiraRepositoryRoute() {
    }

    public JiraRepositoryRoute(
            String id,
            String projectKey,
            String projectName,
            String matchType,
            String matchValue,
            String repository,
            boolean enabled,
            int priority,
            Instant now
    ) {
        this.id = id;
        this.projectKey = projectKey;
        this.projectName = projectName;
        this.matchType = matchType;
        this.matchValue = matchValue;
        this.repository = repository;
        this.enabled = enabled;
        this.priority = priority;
        this.createdAt = now;
        this.updatedAt = now;
    }

    public void update(
            String projectKey,
            String projectName,
            String matchType,
            String matchValue,
            String repository,
            boolean enabled,
            int priority,
            Instant now
    ) {
        this.projectKey = projectKey;
        this.projectName = projectName;
        this.matchType = matchType;
        this.matchValue = matchValue;
        this.repository = repository;
        this.enabled = enabled;
        this.priority = priority;
        this.updatedAt = now;
    }

    public void renameProject(String projectName, Instant now) {
        this.projectName = projectName;
        this.updatedAt = now;
    }

    public String getId() {
        return id;
    }

    public String getProjectKey() {
        return projectKey;
    }

    public String getProjectName() {
        return projectName;
    }

    public String getMatchType() {
        return matchType;
    }

    public String getMatchValue() {
        return matchValue;
    }

    public String getRepository() {
        return repository;
    }

    public boolean isEnabled() {
        return enabled;
    }

    public int getPriority() {
        return priority;
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
