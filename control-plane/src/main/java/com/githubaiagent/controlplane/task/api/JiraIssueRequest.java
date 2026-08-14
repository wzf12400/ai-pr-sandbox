package com.githubaiagent.controlplane.task.api;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;

public record JiraIssueRequest(
        @NotBlank String dataSafetyStatus,
        @NotBlank @Size(max = 32) String sourceReference,
        @NotBlank @Size(max = 255) String issueUrl,
        @NotBlank @Size(max = 24) String projectKey,
        @NotBlank @Size(max = 128) String resolvedRepository,
        @NotBlank @Size(max = 240) String mappingBasis
) {
}
