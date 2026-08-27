package com.githubaiagent.controlplane.task.api;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;

public record ReserveAgentTaskRequest(
        @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String submissionKey,
        @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String issueSha256,
        @NotBlank @Pattern(regexp = "[0-9a-f]{64}") String policySha256
) {
}
