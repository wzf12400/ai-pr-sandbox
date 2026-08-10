package com.githubaiagent.controlplane.routing;

public interface RepositoryMatcher {
    RepositoryMatch match(String sanitizedRequirement);
}
