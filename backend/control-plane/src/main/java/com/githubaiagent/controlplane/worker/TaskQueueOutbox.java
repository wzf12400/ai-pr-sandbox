package com.githubaiagent.controlplane.worker;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;

import java.time.Instant;

@Entity
@Table(name = "task_queue_outbox")
public class TaskQueueOutbox {

    @Id
    @Column(name = "task_id", length = 36, nullable = false)
    private String taskId;

    @Column(nullable = false)
    private long generation;

    @Column(name = "published_generation", nullable = false)
    private long publishedGeneration;

    @Column(name = "publish_attempt_count", nullable = false)
    private int publishAttemptCount;

    @Column(name = "last_error", length = 256)
    private String lastError;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    @Column(name = "published_at")
    private Instant publishedAt;

    @Version
    @Column(nullable = false)
    private long version;

    protected TaskQueueOutbox() {
    }

    public TaskQueueOutbox(String taskId, Instant now) {
        this.taskId = taskId;
        this.generation = 1;
        this.publishedGeneration = 0;
        this.createdAt = now;
        this.updatedAt = now;
    }

    public void reschedule(Instant now) {
        generation++;
        publishAttemptCount = 0;
        lastError = null;
        updatedAt = now;
        publishedAt = null;
    }

    public void markPublished(Instant now) {
        publishedGeneration = generation;
        publishAttemptCount++;
        lastError = null;
        updatedAt = now;
        publishedAt = now;
    }

    public void markPublishFailure(Instant now) {
        publishAttemptCount++;
        lastError = "redis stream temporarily unavailable";
        updatedAt = now;
    }

    public String getTaskId() { return taskId; }
    public long getGeneration() { return generation; }
    public long getPublishedGeneration() { return publishedGeneration; }
    public int getPublishAttemptCount() { return publishAttemptCount; }
    public String getLastError() { return lastError; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }
    public Instant getPublishedAt() { return publishedAt; }
    public long getVersion() { return version; }
}
