package com.githubaiagent.controlplane.task.api;

import java.util.List;

public record TaskDetailResponse(
        TaskResponse task,
        List<TaskEventResponse> events,
        TaskResponse parentTask,
        List<TaskResponse> childTasks
) {
    public TaskDetailResponse {
        events = List.copyOf(events);
        childTasks = List.copyOf(childTasks);
    }
}
