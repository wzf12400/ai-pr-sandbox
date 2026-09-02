package com.githubaiagent.controlplane.task.api;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

public record CreateDependencyTaskRequest(
        @NotBlank
        @Pattern(regexp = "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
        String targetRepository,
        @NotBlank
        @Pattern(regexp = "[A-Z][A-Z0-9_]{1,63}")
        String reasonCode,
        @NotBlank
        @Size(max = 1000)
        String summary
) {
}
