package com.githubaiagent.controlplane.assistant;

import com.githubaiagent.controlplane.task.AutomationJob;

public record TaskConversationContext(
        String sourceType,
        String status,
        String requirement,
        String blockedReason,
        String matchedRepository,
        String issueUrl,
        String prUrl
) {
    public static TaskConversationContext from(AutomationJob job) {
        return new TaskConversationContext(
                job.getSourceType().name(),
                job.getStatus().name(),
                job.getNormalizedRequirement(),
                job.getBlockedReason() == null ? "" : job.getBlockedReason(),
                job.getMatchedRepository() == null ? "" : job.getMatchedRepository(),
                job.getIssueUrl() == null ? "" : job.getIssueUrl(),
                job.getPrUrl() == null ? "" : job.getPrUrl()
        );
    }
}
