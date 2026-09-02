package com.githubaiagent.controlplane.task.api;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

public record TaskProgressRequest(
        @NotBlank
        @Pattern(regexp = "[A-Z][A-Z0-9_]{2,63}")
        String stage,
        @NotBlank
        @Size(max = 900)
        String detail
) {
}
