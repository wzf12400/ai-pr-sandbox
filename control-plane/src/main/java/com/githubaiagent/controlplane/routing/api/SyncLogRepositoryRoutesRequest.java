package com.githubaiagent.controlplane.routing.api;

import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

import java.util.List;

public record SyncLogRepositoryRoutesRequest(
        @NotEmpty
        @Size(max = 50)
        List<
                @Pattern(regexp = "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
                @Size(max = 255)
                String
                > repositories
) {
    public SyncLogRepositoryRoutesRequest {
        repositories = repositories == null ? List.of() : List.copyOf(repositories);
    }
}
