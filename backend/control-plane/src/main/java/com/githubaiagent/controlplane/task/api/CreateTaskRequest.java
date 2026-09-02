package com.githubaiagent.controlplane.task.api;

import com.githubaiagent.controlplane.task.SourceType;
import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

public record CreateTaskRequest(
        @NotNull SourceType sourceType,
        @NotBlank @Size(max = 4000) String input,
        @Valid LogIncidentRequest logIncident,
        @Valid JiraIssueRequest jiraIssue,
        @Pattern(regexp = "[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
        @Size(max = 200) String repositoryHint
) {
    public CreateTaskRequest(SourceType sourceType, String input) {
        this(sourceType, input, null, null, null);
    }

    public CreateTaskRequest(SourceType sourceType, String input, LogIncidentRequest logIncident) {
        this(sourceType, input, logIncident, null, null);
    }

    public CreateTaskRequest(
            SourceType sourceType, String input,
            LogIncidentRequest logIncident, JiraIssueRequest jiraIssue
    ) {
        this(sourceType, input, logIncident, jiraIssue, null);
    }
}
