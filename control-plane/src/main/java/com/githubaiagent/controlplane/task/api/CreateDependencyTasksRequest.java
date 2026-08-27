package com.githubaiagent.controlplane.task.api;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotEmpty;
import jakarta.validation.constraints.Size;

import java.util.List;

public record CreateDependencyTasksRequest(
        @NotEmpty
        @Size(max = 3)
        List<@Valid CreateDependencyTaskRequest> dependencies
) {
    public CreateDependencyTasksRequest {
        dependencies = dependencies == null ? List.of() : List.copyOf(dependencies);
    }
}
