package com.githubaiagent.controlplane.routing.api;

import com.githubaiagent.controlplane.routing.LogRepositoryRoute;

import java.time.Instant;

public record LogRepositoryRouteResponse(
        String id,
        String selectorField,
        String selectorValue,
        String repository,
        boolean enabled,
        long version,
        Instant createdAt,
        Instant updatedAt
) {
    public static LogRepositoryRouteResponse from(LogRepositoryRoute route) {
        return new LogRepositoryRouteResponse(
                route.getId(),
                route.getSelectorField(),
                route.getSelectorValue(),
                route.getRepository(),
                route.isEnabled(),
                route.getVersion(),
                route.getCreatedAt(),
                route.getUpdatedAt()
        );
    }
}
