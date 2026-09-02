package com.githubaiagent.controlplane.routing;

import java.util.List;

public record RepositoryMatch(
        Status status,
        String repository,
        String basis,
        int confidence,
        List<String> candidates
) {
    public RepositoryMatch {
        candidates = List.copyOf(candidates);
    }

    public enum Status {
        RESOLVED,
        NEEDS_CONTEXT
    }
}
