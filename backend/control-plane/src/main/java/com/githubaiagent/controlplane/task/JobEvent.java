package com.githubaiagent.controlplane.task;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

import java.time.Instant;

@Entity
@Table(name = "job_event")
public class JobEvent {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @Column(name = "job_id", length = 36, nullable = false)
    private String jobId;

    @Column(name = "event_type", length = 64, nullable = false)
    private String eventType;

    @Enumerated(EnumType.STRING)
    @Column(name = "from_status", length = 32)
    private TaskStatus fromStatus;

    @Enumerated(EnumType.STRING)
    @Column(name = "to_status", length = 32, nullable = false)
    private TaskStatus toStatus;

    @Enumerated(EnumType.STRING)
    @Column(name = "actor_type", length = 32, nullable = false)
    private ActorType actorType;

    @Column(length = 1000)
    private String detail;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    protected JobEvent() {
    }

    public JobEvent(
            String jobId,
            String eventType,
            TaskStatus fromStatus,
            TaskStatus toStatus,
            ActorType actorType,
            String detail,
            Instant createdAt
    ) {
        this.jobId = jobId;
        this.eventType = eventType;
        this.fromStatus = fromStatus;
        this.toStatus = toStatus;
        this.actorType = actorType;
        this.detail = detail;
        this.createdAt = createdAt;
    }

    public Long getId() { return id; }
    public String getJobId() { return jobId; }
    public String getEventType() { return eventType; }
    public TaskStatus getFromStatus() { return fromStatus; }
    public TaskStatus getToStatus() { return toStatus; }
    public ActorType getActorType() { return actorType; }
    public String getDetail() { return detail; }
    public Instant getCreatedAt() { return createdAt; }
}
