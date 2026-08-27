package com.githubaiagent.controlplane.routing.api;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

public record JiraRepositoryRouteRequest(
        @NotBlank @Size(max = 20) String projectKey,
        @NotBlank @Size(max = 255) String projectName,
        @NotBlank @Size(max = 32) String matchType,
        @NotBlank @Size(max = 255) String matchValue,
        @NotBlank
        @Pattern(regexp = "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
        @Size(max = 255) String repository,
        boolean enabled,
        @Min(0) @Max(10_000) int priority
) {
}
