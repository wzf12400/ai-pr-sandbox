package com.githubaiagent.controlplane.monitoring.api;

import java.time.Instant;
import java.util.Map;

public record MonitorScanCoordinationResponse(
        String scannerKey,
        boolean acquired,
        String leaseToken,
        Instant leaseExpiresAt,
        Map<String, Object> checkpoint,
        Instant lastStartedAt,
        Instant lastCompletedAt,
        String lastError
) {
}
