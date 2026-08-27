package com.githubaiagent.controlplane.configuration.api;

import java.time.Instant;
import java.util.Map;

public record ConfigurationProfileResponse(
        String profileKey,
        Map<String, Object> payload,
        boolean initialized,
        String source,
        long version,
        Instant updatedAt
) {
}
