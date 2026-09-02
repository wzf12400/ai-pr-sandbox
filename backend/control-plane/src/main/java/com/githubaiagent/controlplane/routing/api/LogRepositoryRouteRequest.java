package com.githubaiagent.controlplane.routing.api;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

public record LogRepositoryRouteRequest(
        @NotBlank @Size(max = 64) String selectorField,
        @NotBlank @Size(max = 128) String selectorValue,
        @NotBlank
        @Pattern(regexp = "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
        @Size(max = 255) String repository,
        boolean enabled
) {
}
