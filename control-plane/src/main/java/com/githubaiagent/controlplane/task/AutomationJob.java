package com.githubaiagent.controlplane.task;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.EnumType;
import jakarta.persistence.Enumerated;
import jakarta.persistence.Id;
import jakarta.persistence.Table;
import jakarta.persistence.Version;

import java.time.Instant;

@Entity
@Table(name = "automation_job")
public class AutomationJob {

    @Id
    @Column(length = 36, nullable = false)
    private String id;

    @Enumerated(EnumType.STRING)
    @Column(name = "source_type", length = 32, nullable = false)
    private SourceType sourceType;

    @Enumerated(EnumType.STRING)
    @Column(name = "execution_mode", length = 32, nullable = false)
    private ExecutionMode executionMode;

    @Enumerated(EnumType.STRING)
    @Column(name = "issue_profile", length = 32, nullable = false)
    private IssueProfile issueProfile;

    @Column(name = "input_summary", columnDefinition = "TEXT", nullable = false)
    private String inputSummary;

    @Column(name = "normalized_requirement", columnDefinition = "TEXT", nullable = false)
    private String normalizedRequirement;

    @Column(name = "source_reference", length = 128)
    private String sourceReference;

    @Column(name = "first_seen_at")
    private Instant firstSeenAt;

    @Column(name = "last_seen_at")
    private Instant lastSeenAt;

    @Column(name = "current_scan_event_count")
    private Integer currentScanEventCount;

    @Column(name = "historical_event_count")
    private Integer historicalEventCount;

    @Column(name = "incident_group_count")
    private Integer incidentGroupCount;

    @Column(name = "affected_endpoints", length = 4000)
    private String affectedEndpoints;

    @Column(name = "affected_user_count_min")
    private Integer affectedUserCountMin;

    @Column(name = "affected_user_count_max")
    private Integer affectedUserCountMax;

    @Column(name = "user_identifier_event_count")
    private Integer userIdentifierEventCount;

    @Column(name = "historical_count_complete")
    private Boolean historicalCountComplete;

    @Column(name = "aggregation_basis", length = 1000)
    private String aggregationBasis;

    @Enumerated(EnumType.STRING)
    @Column(length = 32, nullable = false)
    private TaskStatus status;

    @Column(name = "matched_repository", length = 255)
    private String matchedRepository;

    @Column(name = "routing_basis", length = 512)
    private String routingBasis;

    @Column(name = "routing_confidence")
    private Integer routingConfidence;

    @Column(name = "routing_candidates", length = 2000)
    private String routingCandidates;

    @Column(name = "issue_number")
    private Long issueNumber;

    @Column(name = "issue_url", length = 512)
    private String issueUrl;

    @Column(name = "pr_number")
    private Long prNumber;

    @Column(name = "pr_url", length = 512)
    private String prUrl;

    @Column(name = "test_summary", columnDefinition = "TEXT")
    private String testSummary;

    @Column(name = "blocked_reason", length = 1000)
    private String blockedReason;

    @Column(name = "retry_count", nullable = false)
    private int retryCount;

    @Column(name = "submitted_by", length = 128, nullable = false)
    private String submittedBy;

    @Column(name = "policy_id", length = 128, nullable = false)
    private String policyId;

    @Version
    @Column(nullable = false)
    private long version;

    @Column(name = "created_at", nullable = false)
    private Instant createdAt;

    @Column(name = "updated_at", nullable = false)
    private Instant updatedAt;

    protected AutomationJob() {
    }

    public AutomationJob(
            String id,
            SourceType sourceType,
            ExecutionMode executionMode,
            IssueProfile issueProfile,
            String inputSummary,
            String normalizedRequirement,
            String sourceReference,
            Instant firstSeenAt,
            Instant lastSeenAt,
            Integer currentScanEventCount,
            Integer historicalEventCount,
            Integer incidentGroupCount,
            String affectedEndpoints,
            Integer affectedUserCountMin,
            Integer affectedUserCountMax,
            Integer userIdentifierEventCount,
            Boolean historicalCountComplete,
            String aggregationBasis,
            TaskStatus status,
            String matchedRepository,
            String routingBasis,
            Integer routingConfidence,
            String routingCandidates,
            String submittedBy,
            String policyId,
            String blockedReason,
            Instant now
    ) {
        this.id = id;
        this.sourceType = sourceType;
        this.executionMode = executionMode;
        this.issueProfile = issueProfile;
        this.inputSummary = inputSummary;
        this.normalizedRequirement = normalizedRequirement;
        this.sourceReference = sourceReference;
        this.firstSeenAt = firstSeenAt;
        this.lastSeenAt = lastSeenAt;
        this.currentScanEventCount = currentScanEventCount;
        this.historicalEventCount = historicalEventCount;
        this.incidentGroupCount = incidentGroupCount;
        this.affectedEndpoints = affectedEndpoints;
        this.affectedUserCountMin = affectedUserCountMin;
        this.affectedUserCountMax = affectedUserCountMax;
        this.userIdentifierEventCount = userIdentifierEventCount;
        this.historicalCountComplete = historicalCountComplete;
        this.aggregationBasis = aggregationBasis;
        this.status = status;
        this.matchedRepository = matchedRepository;
        this.routingBasis = routingBasis;
        this.routingConfidence = routingConfidence;
        this.routingCandidates = routingCandidates;
        this.submittedBy = submittedBy;
        this.policyId = policyId;
        this.blockedReason = blockedReason;
        this.createdAt = now;
        this.updatedAt = now;
    }

    public void applyRerouting(
            String combinedRequirement,
            String matchedRepository,
            String routingBasis,
            Integer routingConfidence,
            String routingCandidates,
            Instant now
    ) {
        this.normalizedRequirement = combinedRequirement;
        this.matchedRepository = matchedRepository;
        this.routingBasis = routingBasis;
        this.routingConfidence = routingConfidence;
        this.routingCandidates = routingCandidates;
        this.updatedAt = now;
    }

    public void transitionTo(TaskStatus nextStatus, String detail, Instant now) {
        this.status = nextStatus;
        this.updatedAt = now;
        if (nextStatus == TaskStatus.FAILED || nextStatus == TaskStatus.NEEDS_CONTEXT) {
            this.blockedReason = detail;
        } else {
            this.blockedReason = null;
        }
        if (nextStatus == TaskStatus.PENDING && detail != null && !detail.isBlank()) {
            this.retryCount++;
        }
    }

    public void completeMock(String testSummary, Instant now) {
        if (executionMode != ExecutionMode.MOCK) {
            throw new IllegalStateException("only mock tasks may complete without a Draft PR");
        }
        this.status = TaskStatus.COMPLETED;
        this.testSummary = testSummary;
        this.blockedReason = null;
        this.updatedAt = now;
    }

    public boolean attachIssue(long number, String url, Instant now) {
        if (issueNumber != null || issueUrl != null) {
            if (Long.valueOf(number).equals(issueNumber) && url.equals(issueUrl)) {
                return false;
            }
            throw new IllegalArgumentException("task already references a different Issue");
        }
        this.issueNumber = number;
        this.issueUrl = url;
        this.updatedAt = now;
        return true;
    }

    public boolean attachDraftPullRequest(
            long number,
            String url,
            String verifiedTestSummary,
            Instant now
    ) {
        if (prNumber != null || prUrl != null) {
            if (Long.valueOf(number).equals(prNumber) && url.equals(prUrl)) {
                return false;
            }
            throw new IllegalArgumentException("task already references a different Pull Request");
        }
        this.prNumber = number;
        this.prUrl = url;
        this.testSummary = verifiedTestSummary;
        this.updatedAt = now;
        return true;
    }

    public String getId() { return id; }
    public SourceType getSourceType() { return sourceType; }
    public ExecutionMode getExecutionMode() { return executionMode; }
    public IssueProfile getIssueProfile() { return issueProfile; }
    public String getInputSummary() { return inputSummary; }
    public String getNormalizedRequirement() { return normalizedRequirement; }
    public String getSourceReference() { return sourceReference; }
    public Instant getFirstSeenAt() { return firstSeenAt; }
    public Instant getLastSeenAt() { return lastSeenAt; }
    public Integer getCurrentScanEventCount() { return currentScanEventCount; }
    public Integer getHistoricalEventCount() { return historicalEventCount; }
    public Integer getIncidentGroupCount() { return incidentGroupCount; }
    public String getAffectedEndpoints() { return affectedEndpoints; }
    public Integer getAffectedUserCountMin() { return affectedUserCountMin; }
    public Integer getAffectedUserCountMax() { return affectedUserCountMax; }
    public Integer getUserIdentifierEventCount() { return userIdentifierEventCount; }
    public Boolean getHistoricalCountComplete() { return historicalCountComplete; }
    public String getAggregationBasis() { return aggregationBasis; }
    public TaskStatus getStatus() { return status; }
    public String getMatchedRepository() { return matchedRepository; }
    public String getRoutingBasis() { return routingBasis; }
    public Integer getRoutingConfidence() { return routingConfidence; }
    public String getRoutingCandidates() { return routingCandidates; }
    public Long getIssueNumber() { return issueNumber; }
    public String getIssueUrl() { return issueUrl; }
    public Long getPrNumber() { return prNumber; }
    public String getPrUrl() { return prUrl; }
    public String getTestSummary() { return testSummary; }
    public String getBlockedReason() { return blockedReason; }
    public int getRetryCount() { return retryCount; }
    public String getSubmittedBy() { return submittedBy; }
    public String getPolicyId() { return policyId; }
    public long getVersion() { return version; }
    public Instant getCreatedAt() { return createdAt; }
    public Instant getUpdatedAt() { return updatedAt; }
}
