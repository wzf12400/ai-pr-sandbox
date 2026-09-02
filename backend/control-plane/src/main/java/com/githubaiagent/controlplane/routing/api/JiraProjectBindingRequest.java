package com.githubaiagent.controlplane.routing.api;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

public record JiraProjectBindingRequest(
        @NotBlank
        @Pattern(regexp = "[A-Z][A-Z0-9_]{0,19}")
        @Size(max = 20) String projectKey,
        @NotBlank @Size(max = 255) String projectName,
        @NotBlank
        @Pattern(regexp = "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
        @Size(max = 255) String repository
) {
}
