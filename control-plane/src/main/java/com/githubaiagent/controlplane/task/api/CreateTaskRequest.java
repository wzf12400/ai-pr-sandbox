package com.githubaiagent.controlplane.task.api;

import com.githubaiagent.controlplane.task.SourceType;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Size;

public record CreateTaskRequest(
        @NotNull SourceType sourceType,
        @NotBlank @Size(max = 4000) String input,
        @Valid LogIncidentRequest logIncident,
        @Valid JiraIssueRequest jiraIssue
) {
    public CreateTaskRequest(SourceType sourceType, String input) {
        this(sourceType, input, null, null);
    }

    public CreateTaskRequest(SourceType sourceType, String input, LogIncidentRequest logIncident) {
        this(sourceType, input, logIncident, null);
    }
}
