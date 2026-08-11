package com.githubaiagent.controlplane.task.api;

import com.githubaiagent.controlplane.task.ActorType;
import com.githubaiagent.controlplane.task.JobEvent;
import com.githubaiagent.controlplane.task.TaskStatus;

import java.time.Instant;

public record TaskEventResponse(
        Long id,
        String eventType,
        TaskStatus fromStatus,
        TaskStatus toStatus,
        ActorType actorType,
        String detail,
        Instant createdAt
) {
    public static TaskEventResponse from(JobEvent event) {
        return new TaskEventResponse(
                event.getId(),
                event.getEventType(),
                event.getFromStatus(),
                event.getToStatus(),
                event.getActorType(),
                event.getDetail(),
                event.getCreatedAt()
        );
    }
}
