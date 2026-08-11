package com.githubaiagent.controlplane.task.api;

import java.util.List;

public record TaskDetailResponse(
        TaskResponse task,
        List<TaskEventResponse> events
) {
    public TaskDetailResponse {
        events = List.copyOf(events);
    }
}
