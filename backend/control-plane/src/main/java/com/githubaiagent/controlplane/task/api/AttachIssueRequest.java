package com.githubaiagent.controlplane.task.api;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;

public record AttachIssueRequest(
        @Positive long issueNumber,
        @NotBlank @Size(max = 512) String issueUrl
) {
}
