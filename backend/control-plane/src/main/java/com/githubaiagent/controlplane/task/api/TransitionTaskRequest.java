package com.githubaiagent.controlplane.task.api;

import com.githubaiagent.controlplane.task.TaskStatus;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

public record TransitionTaskRequest(
        @NotNull TaskStatus targetStatus,
        @Size(max = 1000) String detail
) {
}
