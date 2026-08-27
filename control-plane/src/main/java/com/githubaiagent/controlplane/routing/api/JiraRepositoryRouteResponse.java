package com.githubaiagent.controlplane.routing.api;

import com.githubaiagent.controlplane.routing.JiraRepositoryRoute;

import java.time.Instant;

public record JiraRepositoryRouteResponse(
        String id,
        String projectKey,
        String projectName,
        String matchType,
        String matchValue,
        String repository,
        boolean enabled,
        int priority,
        long version,
        Instant createdAt,
        Instant updatedAt
) {
    public static JiraRepositoryRouteResponse from(JiraRepositoryRoute route) {
        return new JiraRepositoryRouteResponse(
                route.getId(),
                route.getProjectKey(),
                route.getProjectName(),
                route.getMatchType(),
                route.getMatchValue(),
                route.getRepository(),
                route.isEnabled(),
                route.getPriority(),
                route.getVersion(),
                route.getCreatedAt(),
                route.getUpdatedAt()
        );
    }
}
