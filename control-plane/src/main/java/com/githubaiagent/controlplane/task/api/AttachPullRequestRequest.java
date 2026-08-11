package com.githubaiagent.controlplane.task.api;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.Size;

public record AttachPullRequestRequest(
        @Positive long prNumber,
        @NotBlank @Size(max = 512) String prUrl,
        @NotBlank @Size(max = 2000) String testSummary
) {
}
