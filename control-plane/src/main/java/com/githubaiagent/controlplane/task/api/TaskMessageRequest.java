package com.githubaiagent.controlplane.task.api;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record TaskMessageRequest(
        @NotBlank @Size(max = 1000) String content
) {
}
