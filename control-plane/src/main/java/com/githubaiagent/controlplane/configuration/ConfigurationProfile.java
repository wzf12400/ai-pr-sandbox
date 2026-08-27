package com.githubaiagent.controlplane.configuration;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;

import java.time.Instant;

@Entity
@Table(name = "configuration_profiles")
public class ConfigurationProfile {

    @Id
    @Column(name = "profile_key", length = 64, nullable = false)
    private String profileKey;

    @Column(name = "payload_json", columnDefinition = "JSON", nullable = false)
    private String payloadJson;

    @Column(nullable = false)
    private boolean initialized;

    @Column(length = 64, nullable = false)
    private String source;

    @Version
    @Column(nullable = false)
    private long version;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected ConfigurationProfile() {
    }

    public void replaceSnapshot(String payloadJson, String source, Instant now) {
        this.payloadJson = payloadJson;
        this.initialized = true;
        this.source = source;
        this.updatedAt = now;
    }

    public String getProfileKey() {
        return profileKey;
    }

    public String getPayloadJson() {
        return payloadJson;
    }

    public boolean isInitialized() {
        return initialized;
    }

    public String getSource() {
        return source;
    }

    public long getVersion() {
        return version;
    }

    public Instant getUpdatedAt() {
        return updatedAt;
    }
}
