package com.githubaiagent.controlplane.monitoring;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;

import java.time.Instant;

@Entity
@Table(name = "monitor_scan_coordination")
public class MonitorScanCoordination {

    @Id
    @Column(name = "scanner_key", length = 16, nullable = false)
    private String scannerKey;

    @Column(name = "lease_owner", length = 128)
    private String leaseOwner;

    @Column(name = "lease_token", length = 36)
    private String leaseToken;

    @Column(name = "lease_expires_at")
    private Instant leaseExpiresAt;

    @Column(name = "checkpoint_json", columnDefinition = "MEDIUMTEXT")
    private String checkpointJson;

    @Column(name = "last_started_at")
    private Instant lastStartedAt;

    @Column(name = "last_completed_at")
    private Instant lastCompletedAt;

    @Column(name = "last_error", length = 500)
    private String lastError;

    @Version
    @Column(nullable = false)
    private long version;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected MonitorScanCoordination() {
    }

    public boolean hasActiveLease(Instant now) {
        return leaseExpiresAt != null && leaseExpiresAt.isAfter(now);
    }

    public void acquire(
            String owner,
            String token,
            Instant expiresAt,
            Instant now
    ) {
        leaseOwner = owner;
        leaseToken = token;
        leaseExpiresAt = expiresAt;
        lastStartedAt = now;
        updatedAt = now;
    }

    public void complete(String checkpoint, String error, Instant now) {
        checkpointJson = checkpoint;
        lastCompletedAt = now;
        lastError = error;
        leaseOwner = null;
        leaseToken = null;
        leaseExpiresAt = null;
        updatedAt = now;
    }

    public String getScannerKey() {
        return scannerKey;
    }

    public String getLeaseToken() {
        return leaseToken;
    }

    public Instant getLeaseExpiresAt() {
        return leaseExpiresAt;
    }

    public String getCheckpointJson() {
        return checkpointJson;
    }

    public Instant getLastStartedAt() {
        return lastStartedAt;
    }

    public Instant getLastCompletedAt() {
        return lastCompletedAt;
    }

    public String getLastError() {
        return lastError;
    }
}
