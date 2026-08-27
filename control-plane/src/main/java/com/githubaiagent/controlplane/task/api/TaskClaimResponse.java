package com.githubaiagent.controlplane.task.api;

import com.githubaiagent.controlplane.task.AutomationJob;
import com.githubaiagent.controlplane.task.ExecutionMode;
import com.githubaiagent.controlplane.task.IssueProfile;
import com.githubaiagent.controlplane.task.SourceType;

public record TaskClaimResponse(
        String taskId,
        SourceType sourceType,
        ExecutionMode executionMode,
        IssueProfile issueProfile,
        String inputSummary,
        String normalizedRequirement,
        String matchedRepository,
        String routingBasis,
        Integer routingConfidence,
        String policyId,
        LogIncidentView logIncident,
        Long issueNumber,
        String issueUrl,
        String agentTaskId,
        String agentTaskUrl,
        String agentSubmissionKey,
        String agentIssueSha256,
        String agentPolicySha256
) {
    public static TaskClaimResponse from(AutomationJob job) {
        return new TaskClaimResponse(
                job.getId(),
                job.getSourceType(),
                job.getExecutionMode(),
                job.getIssueProfile(),
                job.getInputSummary(),
                job.getNormalizedRequirement(),
                job.getMatchedRepository(),
                job.getRoutingBasis(),
                job.getRoutingConfidence(),
                job.getPolicyId(),
                LogIncidentView.from(job),
                job.getIssueNumber(),
                job.getIssueUrl(),
                job.getAgentTaskId(),
                job.getAgentTaskUrl(),
                job.getAgentSubmissionKey(),
                job.getAgentIssueSha256(),
                job.getAgentPolicySha256()
        );
    }
}
