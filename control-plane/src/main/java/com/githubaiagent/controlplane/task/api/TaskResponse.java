package com.githubaiagent.controlplane.task.api;

import com.githubaiagent.controlplane.task.AutomationJob;
import com.githubaiagent.controlplane.task.ExecutionMode;
import com.githubaiagent.controlplane.task.IssueProfile;
import com.githubaiagent.controlplane.task.SourceType;
import com.githubaiagent.controlplane.task.TaskStatus;

import java.time.Instant;
import java.util.Arrays;
import java.util.List;

public record TaskResponse(
        String id,
        SourceType sourceType,
        ExecutionMode executionMode,
        IssueProfile issueProfile,
        String inputSummary,
        String normalizedRequirement,
        TaskStatus status,
        String matchedRepository,
        String routingBasis,
        Integer routingConfidence,
        List<String> routingCandidates,
        Long issueNumber,
        String issueUrl,
        Long prNumber,
        String prUrl,
        String testSummary,
        String blockedReason,
        int retryCount,
        String submittedBy,
        String policyId,
        LogIncidentView logIncident,
        Instant createdAt,
        Instant updatedAt
) {
    public static TaskResponse from(AutomationJob job) {
        List<String> candidates = job.getRoutingCandidates() == null
                || job.getRoutingCandidates().isBlank()
                ? List.of()
                : Arrays.asList(job.getRoutingCandidates().split(","));
        return new TaskResponse(
                job.getId(),
                job.getSourceType(),
                job.getExecutionMode(),
                job.getIssueProfile(),
                job.getInputSummary(),
                job.getNormalizedRequirement(),
                job.getStatus(),
                job.getMatchedRepository(),
                job.getRoutingBasis(),
                job.getRoutingConfidence(),
                candidates,
                job.getIssueNumber(),
                job.getIssueUrl(),
                job.getPrNumber(),
                job.getPrUrl(),
                job.getTestSummary(),
                job.getBlockedReason(),
                job.getRetryCount(),
                job.getSubmittedBy(),
                job.getPolicyId(),
                LogIncidentView.from(job),
                job.getCreatedAt(),
                job.getUpdatedAt()
        );
    }
}
