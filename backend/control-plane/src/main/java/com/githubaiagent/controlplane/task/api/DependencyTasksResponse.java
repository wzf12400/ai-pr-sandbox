package com.githubaiagent.controlplane.task.api;

import java.util.List;

public record DependencyTasksResponse(List<TaskResponse> tasks) {
    public DependencyTasksResponse {
        tasks = List.copyOf(tasks);
    }
}
