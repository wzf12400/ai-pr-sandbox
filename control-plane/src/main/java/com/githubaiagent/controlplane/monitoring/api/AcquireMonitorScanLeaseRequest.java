package com.githubaiagent.controlplane.monitoring.api;

import jakarta.validation.constraints.Max;
import jakarta.validation.constraints.Min;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

public record AcquireMonitorScanLeaseRequest(
        @NotBlank
        @Pattern(regexp = "[A-Za-z0-9._:-]+")
        @Size(max = 128)
        String ownerId,
        @Min(30) @Max(900) int leaseSeconds
) {
}
