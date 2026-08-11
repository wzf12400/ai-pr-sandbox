package com.githubaiagent.controlplane.task;

import com.githubaiagent.controlplane.config.AppProperties;
import com.githubaiagent.controlplane.routing.RepositoryMatch;
import com.githubaiagent.controlplane.routing.RepositoryMatcher;
import com.githubaiagent.controlplane.task.api.CreateTaskRequest;
import com.githubaiagent.controlplane.task.api.LogIncidentRequest;
import com.githubaiagent.controlplane.task.api.TaskClaimResponse;
import com.githubaiagent.controlplane.task.api.TaskDetailResponse;
import com.githubaiagent.controlplane.task.api.TaskEventResponse;
import com.githubaiagent.controlplane.task.api.TaskResponse;
import com.githubaiagent.controlplane.worker.TaskClaimConflictException;
import com.githubaiagent.controlplane.worker.TaskQueueOutboxService;
import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.EnumMap;
import java.util.EnumSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;

@Service
public class TaskService {

    private static final Map<TaskStatus, Set<TaskStatus>> ALLOWED_TRANSITIONS = allowedTransitions();
    private static final Pattern SAFE_LOG_REFERENCE = Pattern.compile(
            "(?:incident_ref|event_ref):[0-9a-f]{16,64}"
    );

    private final AutomationJobRepository jobRepository;
    private final JobEventRepository eventRepository;
    private final NaturalLanguageSanitizer sanitizer;
    private final RepositoryMatcher repositoryMatcher;
    private final AppProperties properties;
    private final TaskQueueOutboxService outboxService;

    public TaskService(
            AutomationJobRepository jobRepository,
            JobEventRepository eventRepository,
            NaturalLanguageSanitizer sanitizer,
            RepositoryMatcher repositoryMatcher,
            AppProperties properties,
            TaskQueueOutboxService outboxService
    ) {
        this.jobRepository = jobRepository;
        this.eventRepository = eventRepository;
        this.sanitizer = sanitizer;
        this.repositoryMatcher = repositoryMatcher;
        this.properties = properties;
        this.outboxService = outboxService;
    }

    @Transactional
    public TaskResponse create(CreateTaskRequest request) {
        if (request.sourceType() == SourceType.JIRA) {
            throw new IllegalArgumentException(
                    "Jira tasks require a separately sanitized issue-intake record"
            );
        }
        String sanitizedInput = sanitizer.sanitize(request.input());
        if (sanitizedInput.isBlank()) {
            throw new IllegalArgumentException("task input is empty after sanitization");
        }
        IssueProfile issueProfile = IssueProfile.NATURAL_LANGUAGE;
        LogIncidentRequest logIncident = null;
        if (request.sourceType() == SourceType.LOG) {
            logIncident = validateLogIncident(request, sanitizedInput);
            issueProfile = IssueProfile.LOG_INCIDENT;
            var existingLogTask = jobRepository
                    .findFirstBySourceReferenceOrderByCreatedAtAsc(
                            logIncident.sourceReference()
                    );
            if (existingLogTask.isPresent()) {
                return TaskResponse.from(existingLogTask.get());
            }
        } else if (request.logIncident() != null) {
            throw new IllegalArgumentException(
                    "logIncident is allowed only when sourceType is LOG"
            );
        }

        RepositoryMatch match = repositoryMatcher.match(sanitizedInput);
        TaskStatus initialStatus = match.status() == RepositoryMatch.Status.RESOLVED
                ? TaskStatus.PENDING
                : TaskStatus.NEEDS_CONTEXT;
        String blockedReason = initialStatus == TaskStatus.NEEDS_CONTEXT ? match.basis() : null;
        Instant now = Instant.now();
        AutomationJob job = new AutomationJob(
                UUID.randomUUID().toString(),
                request.sourceType(),
                ExecutionMode.MOCK,
                issueProfile,
                sanitizedInput,
                sanitizedInput,
                logIncident == null ? null : logIncident.sourceReference(),
                logIncident == null ? null : logIncident.firstSeenAt(),
                logIncident == null ? null : logIncident.lastSeenAt(),
                logIncident == null ? null : logIncident.currentScanEventCount(),
                logIncident == null ? null : logIncident.historicalEventCount(),
                logIncident == null ? null : logIncident.incidentGroupCount(),
                logIncident == null ? null : String.join("\n", logIncident.affectedEndpoints()),
                logIncident == null ? null : logIncident.affectedUserCountMin(),
                logIncident == null ? null : logIncident.affectedUserCountMax(),
                logIncident == null ? null : logIncident.userIdentifierEventCount(),
                logIncident == null ? null : logIncident.historicalCountComplete(),
                logIncident == null ? null : logIncident.aggregationBasis(),
                initialStatus,
                match.repository(),
                match.basis(),
                match.confidence(),
                String.join(",", match.candidates()),
                "local-user",
                properties.policyId(),
                blockedReason,
                now
        );
        jobRepository.save(job);
        eventRepository.save(new JobEvent(
                job.getId(),
                "TASK_CREATED",
                null,
                initialStatus,
                ActorType.SYSTEM,
                match.basis(),
                now
        ));
        if (initialStatus == TaskStatus.PENDING) {
            outboxService.schedule(job.getId(), now);
        }
        return TaskResponse.from(job);
    }

    private LogIncidentRequest validateLogIncident(
            CreateTaskRequest request,
            String sanitizedInput
    ) {
        LogIncidentRequest incident = request.logIncident();
        if (incident == null) {
            throw new IllegalArgumentException("LOG tasks require logIncident evidence");
        }
        if (!"SANITIZED".equals(incident.dataSafetyStatus())) {
            throw new IllegalArgumentException(
                    "LOG tasks accept only SANITIZED incident evidence"
            );
        }
        if (!sanitizedInput.equals(request.input().trim())) {
            throw new IllegalArgumentException(
                    "LOG incident summary still contains content requiring redaction"
            );
        }
        if (!SAFE_LOG_REFERENCE.matcher(incident.sourceReference()).matches()) {
            throw new IllegalArgumentException("LOG sourceReference is invalid");
        }
        if (incident.firstSeenAt().isAfter(incident.lastSeenAt())) {
            throw new IllegalArgumentException("firstSeenAt must not be after lastSeenAt");
        }
        if (incident.currentScanEventCount() > incident.historicalEventCount()) {
            throw new IllegalArgumentException(
                    "currentScanEventCount must not exceed historicalEventCount"
            );
        }
        if (incident.incidentGroupCount() > incident.historicalEventCount()) {
            throw new IllegalArgumentException(
                    "incidentGroupCount must not exceed historicalEventCount"
            );
        }
        Integer userMin = incident.affectedUserCountMin();
        Integer userMax = incident.affectedUserCountMax();
        if ((userMin == null) != (userMax == null)
                || userMin != null && userMin > userMax) {
            throw new IllegalArgumentException("affected user count range is invalid");
        }
        if (incident.userIdentifierEventCount() > incident.historicalEventCount()) {
            throw new IllegalArgumentException(
                    "userIdentifierEventCount must not exceed historicalEventCount"
            );
        }
        if (!sanitizer.sanitize(incident.aggregationBasis())
                .equals(incident.aggregationBasis().trim())) {
            throw new IllegalArgumentException(
                    "aggregationBasis still contains content requiring redaction"
            );
        }
        for (String endpoint : incident.affectedEndpoints()) {
            if (!sanitizer.sanitize(endpoint).equals(endpoint.trim())) {
                throw new IllegalArgumentException(
                        "affectedEndpoints still contain content requiring redaction"
                );
            }
        }
        return incident;
    }

    @Transactional(readOnly = true)
    public List<TaskResponse> list() {
        return jobRepository.findAllByOrderByCreatedAtDesc(PageRequest.of(0, 100)).stream()
                .map(TaskResponse::from)
                .toList();
    }

    @Transactional(readOnly = true)
    public TaskDetailResponse detail(String taskId) {
        AutomationJob job = findJob(taskId);
        List<TaskEventResponse> events = eventRepository.findByJobIdOrderByCreatedAtAscIdAsc(taskId)
                .stream()
                .map(TaskEventResponse::from)
                .toList();
        return new TaskDetailResponse(TaskResponse.from(job), events);
    }

    @Transactional
    public TaskResponse transition(
            String taskId,
            TaskStatus targetStatus,
            String detail,
            ActorType actorType
    ) {
        AutomationJob job = findJobForUpdate(taskId);
        TaskStatus currentStatus = job.getStatus();
        if (!ALLOWED_TRANSITIONS.getOrDefault(currentStatus, Set.of()).contains(targetStatus)) {
            throw new InvalidTaskTransitionException(currentStatus, targetStatus);
        }
        Instant now = Instant.now();
        if (currentStatus == TaskStatus.TESTING && targetStatus == TaskStatus.COMPLETED) {
            if (actorType != ActorType.MOCK_WORKER || job.getExecutionMode() != ExecutionMode.MOCK) {
                throw new InvalidTaskTransitionException(currentStatus, targetStatus);
            }
            job.completeMock(detail, now);
        } else {
            job.transitionTo(targetStatus, detail, now);
        }
        eventRepository.save(new JobEvent(
                taskId,
                "STATUS_CHANGED",
                currentStatus,
                targetStatus,
                actorType,
                detail,
                now
        ));
        if (targetStatus == TaskStatus.PENDING) {
            outboxService.schedule(taskId, now);
        }
        return TaskResponse.from(job);
    }

    @Transactional
    public TaskClaimResponse claim(String taskId) {
        AutomationJob job = findJobForUpdate(taskId);
        if (job.getStatus() != TaskStatus.PENDING) {
            throw new TaskClaimConflictException(taskId, job.getStatus());
        }
        TaskStatus previous = job.getStatus();
        Instant now = Instant.now();
        job.transitionTo(TaskStatus.PROCESSING, "mock worker claimed task", now);
        eventRepository.save(new JobEvent(
                taskId,
                "TASK_CLAIMED",
                previous,
                TaskStatus.PROCESSING,
                ActorType.MOCK_WORKER,
                "mock worker claimed task",
                now
        ));
        return TaskClaimResponse.from(job);
    }

    @Transactional
    public TaskResponse attachIssue(String taskId, long issueNumber, String issueUrl) {
        AutomationJob job = findJobForUpdate(taskId);
        if (job.getStatus() != TaskStatus.PROCESSING) {
            throw new IllegalArgumentException("Issue may only be attached while task is PROCESSING");
        }
        String expectedUrl = "https://github.com/" + job.getMatchedRepository()
                + "/issues/" + issueNumber;
        if (!expectedUrl.equals(issueUrl)) {
            throw new IllegalArgumentException("Issue URL does not match the task repository and number");
        }
        Instant now = Instant.now();
        if (job.attachIssue(issueNumber, issueUrl, now)) {
            eventRepository.save(new JobEvent(
                    taskId,
                    "ISSUE_LINKED",
                    TaskStatus.PROCESSING,
                    TaskStatus.PROCESSING,
                    ActorType.MOCK_WORKER,
                    "GitHub Issue reference recorded",
                    now
            ));
        }
        return TaskResponse.from(job);
    }

    @Transactional
    public TaskResponse attachPullRequest(
            String taskId,
            long prNumber,
            String prUrl,
            String testSummary
    ) {
        AutomationJob job = findJobForUpdate(taskId);
        if (job.getStatus() != TaskStatus.TESTING) {
            throw new IllegalArgumentException(
                    "Draft PR may only be attached after policy tests enter TESTING"
            );
        }
        if (job.getIssueNumber() == null || job.getIssueUrl() == null) {
            throw new IllegalArgumentException("Draft PR requires a recorded GitHub Issue");
        }
        String expectedUrl = "https://github.com/" + job.getMatchedRepository()
                + "/pull/" + prNumber;
        if (!expectedUrl.equals(prUrl)) {
            throw new IllegalArgumentException(
                    "Pull Request URL does not match the task repository and number"
            );
        }
        Instant now = Instant.now();
        if (job.attachDraftPullRequest(prNumber, prUrl, testSummary, now)) {
            eventRepository.save(new JobEvent(
                    taskId,
                    "DRAFT_PR_LINKED",
                    TaskStatus.TESTING,
                    TaskStatus.TESTING,
                    ActorType.MOCK_WORKER,
                    "tested Draft PR reference recorded",
                    now
            ));
        }
        return TaskResponse.from(job);
    }

    @Transactional
    public void requeue(String taskId) {
        AutomationJob job = findJobForUpdate(taskId);
        if (job.getStatus() != TaskStatus.PENDING) {
            throw new TaskClaimConflictException(taskId, job.getStatus());
        }
        outboxService.schedule(taskId, Instant.now());
    }

    private AutomationJob findJob(String taskId) {
        return jobRepository.findById(taskId)
                .orElseThrow(() -> new TaskNotFoundException(taskId));
    }

    private AutomationJob findJobForUpdate(String taskId) {
        return jobRepository.findByIdForUpdate(taskId)
                .orElseThrow(() -> new TaskNotFoundException(taskId));
    }

    private static Map<TaskStatus, Set<TaskStatus>> allowedTransitions() {
        EnumMap<TaskStatus, Set<TaskStatus>> transitions = new EnumMap<>(TaskStatus.class);
        transitions.put(TaskStatus.PENDING, EnumSet.of(
                TaskStatus.PROCESSING, TaskStatus.NEEDS_CONTEXT, TaskStatus.FAILED
        ));
        transitions.put(TaskStatus.PROCESSING, EnumSet.of(
                TaskStatus.TESTING, TaskStatus.NEEDS_CONTEXT, TaskStatus.FAILED
        ));
        transitions.put(TaskStatus.TESTING, EnumSet.of(
                TaskStatus.AWAITING_PR_REVIEW,
                TaskStatus.COMPLETED,
                TaskStatus.NEEDS_CONTEXT,
                TaskStatus.FAILED
        ));
        transitions.put(TaskStatus.AWAITING_PR_REVIEW, EnumSet.of(
                TaskStatus.PROCESSING, TaskStatus.COMPLETED, TaskStatus.FAILED
        ));
        transitions.put(TaskStatus.NEEDS_CONTEXT, EnumSet.of(TaskStatus.PENDING, TaskStatus.FAILED));
        transitions.put(TaskStatus.FAILED, EnumSet.of(TaskStatus.PENDING));
        transitions.put(TaskStatus.COMPLETED, EnumSet.noneOf(TaskStatus.class));
        return Map.copyOf(transitions);
    }
}
