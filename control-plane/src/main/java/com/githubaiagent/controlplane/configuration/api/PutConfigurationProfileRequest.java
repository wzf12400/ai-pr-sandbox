package com.githubaiagent.controlplane.configuration.api;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.PositiveOrZero;
import jakarta.validation.constraints.Size;

import java.util.Map;

public record PutConfigurationProfileRequest(
        @NotNull @PositiveOrZero Long expectedVersion,
        @NotNull Map<String, Object> payload,
        @NotBlank
        @Size(max = 64)
        @jakarta.validation.constraints.Pattern(
                regexp = "[A-Za-z][A-Za-z0-9._-]*",
                message = "must use letters, digits, '.', '_' or '-' and start with a letter"
        )
        String source
) {
}
