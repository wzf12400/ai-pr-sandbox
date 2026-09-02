package com.githubaiagent.controlplane.task.api;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record AttachAgentTaskRequest(
        @NotBlank @Size(max = 160) String agentTaskId,
        @NotBlank @Size(max = 512) String agentTaskUrl
) {
}
