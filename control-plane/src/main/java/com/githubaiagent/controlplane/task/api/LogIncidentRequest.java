package com.githubaiagent.controlplane.task.api;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.PositiveOrZero;
import jakarta.validation.constraints.Size;

import java.time.Instant;
import java.util.List;

public record LogIncidentRequest(
        @NotBlank String dataSafetyStatus,
        @NotBlank @Size(max = 128) String sourceReference,
        @NotNull Instant firstSeenAt,
        @NotNull Instant lastSeenAt,
        @Positive int currentScanEventCount,
        @Positive int historicalEventCount,
        @Positive int incidentGroupCount,
        @Size(max = 50) List<@NotBlank @Size(max = 255) String> affectedEndpoints,
        @PositiveOrZero Integer affectedUserCountMin,
        @PositiveOrZero Integer affectedUserCountMax,
        @PositiveOrZero int userIdentifierEventCount,
        boolean historicalCountComplete,
        @NotBlank @Size(max = 1000) String aggregationBasis
) {
    public LogIncidentRequest {
        affectedEndpoints = affectedEndpoints == null ? List.of() : List.copyOf(affectedEndpoints);
    }
}
