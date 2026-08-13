package com.githubaiagent.controlplane.task;

import com.githubaiagent.controlplane.assistant.AssistantAnswer;
import com.githubaiagent.controlplane.assistant.AssistantService;
import com.githubaiagent.controlplane.assistant.TaskConversationContext;
import com.githubaiagent.controlplane.config.AppProperties;
import com.githubaiagent.controlplane.routing.RepositoryMatch;
import com.githubaiagent.controlplane.routing.RepositoryMatcher;
import com.githubaiagent.controlplane.task.api.CreateTaskRequest;
import com.githubaiagent.controlplane.task.api.LogIncidentRequest;
import com.githubaiagent.controlplane.task.api.TaskClaimResponse;
import com.githubaiagent.controlplane.task.api.TaskDetailResponse;
import com.githubaiagent.controlplane.task.api.TaskEventResponse;
import com.githubaiagent.controlplane.task.api.TaskMessageRequest;
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
import java.util.Optional;
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
    private final AssistantService assistantService;

    public TaskService(
            AutomationJobRepository jobRepository,
            JobEventRepository eventRepository,
            NaturalLanguageSanitizer sanitizer,
            RepositoryMatcher repositoryMatcher,
            AppProperties properties,
            TaskQueueOutboxService outboxService,
            AssistantService assistantService
    ) {
        this.jobRepository = jobRepository;
        this.eventRepository = eventRepository;
        this.sanitizer = sanitizer;
        this.repositoryMatcher = repositoryMatcher;
        this.properties = properties;
        this.outboxService = outboxService;
        this.assistantService = assistantService;
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
        String requirement = sanitizedInput;
        Optional<AssistantAnswer> opening = Optional.empty();
        if (match.status() != RepositoryMatch.Status.RESOLVED) {
            opening = assistantService.converse(
                    new TaskConversationContext(
                            request.sourceType().name(),
                            TaskStatus.NEEDS_CONTEXT.name(),
                            sanitizedInput,
                            match.basis(),
                            "",
                            "",
                            ""
                    ),
                    List.of(),
                    sanitizedInput
            );
            if (opening.isPresent() && !opening.get().routingHints().isEmpty()) {
                String withHints = appendCapped(
                        sanitizedInput,
                        String.join(" ", opening.get().routingHints())
                );
                RepositoryMatch hintedMatch = repositoryMatcher.match(withHints);
                if (hintedMatch.status() == RepositoryMatch.Status.RESOLVED) {
                    requirement = withHints;
                    match = hintedMatch;
                }
            }
        }
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
                requirement,
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
            if (opening.isPresent()) {
                eventRepository.save(new JobEvent(
                        job.getId(),
                        "AGENT_REPLY",
                        initialStatus,
                        initialStatus,
                        ActorType.ASSISTANT,
                        "已根据你的描述路由到授权仓库 " + match.repository()
                                + "（" + match.basis() + "），任务已排队。",
                        now
                ));
            }
        } else {
            RepositoryMatch unresolvedMatch = match;
            String openingReply = opening
                    .map(AssistantAnswer::reply)
                    .orElseGet(() -> buildMissingContextReply(unresolvedMatch));
            eventRepository.save(new JobEvent(
                    job.getId(),
                    "AGENT_REPLY",
                    initialStatus,
                    initialStatus,
                    ActorType.ASSISTANT,
                    openingReply,
                    now
            ));
        }
        return TaskResponse.from(job);
    }

    @Transactional
    public TaskDetailResponse postMessage(String taskId, TaskMessageRequest request) {
        String sanitized = sanitizer.sanitize(request.content());
        if (sanitized.isBlank()) {
            throw new IllegalArgumentException("message is empty after sanitization");
        }
        AutomationJob job = findJobForUpdate(taskId);
        TaskStatus status = job.getStatus();
        List<JobEvent> priorEvents =
                eventRepository.findByJobIdOrderByCreatedAtAscIdAsc(taskId);
        Instant now = Instant.now();
        eventRepository.save(new JobEvent(
                taskId,
                "USER_MESSAGE",
                status,
                status,
                ActorType.USER,
                sanitized,
                now
        ));

        String reply;
        if (status == TaskStatus.NEEDS_CONTEXT || status == TaskStatus.FAILED) {
            reply = rerouteWithSuppliedContext(job, sanitized, priorEvents, status, now);
        } else if (status == TaskStatus.COMPLETED) {
            reply = assistantService.converse(job, priorEvents, sanitized)
                    .map(AssistantAnswer::reply)
                    .orElse("该任务已完成。如有新的变更需求，请直接描述，我会创建新任务。");
        } else {
            String fallback = "收到，补充信息已记录到事件流。任务当前处于「" + status
                    + "」状态，流水线处理中，不会被对话打断。";
            reply = assistantService.converse(job, priorEvents, sanitized)
                    .map(AssistantAnswer::reply)
                    .orElse(fallback);
        }
        eventRepository.save(new JobEvent(
                taskId,
                "AGENT_REPLY",
                status,
                job.getStatus(),
                ActorType.ASSISTANT,
                reply,
                now
        ));
        return detail(taskId);
    }

    private String rerouteWithSuppliedContext(
            AutomationJob job,
            String sanitized,
            List<JobEvent> priorEvents,
            TaskStatus status,
            Instant now
    ) {
        String combined = appendCapped(job.getNormalizedRequirement(), sanitized);
        RepositoryMatch match = repositoryMatcher.match(combined);
        Optional<AssistantAnswer> answer = Optional.empty();
        if (match.status() != RepositoryMatch.Status.RESOLVED) {
            answer = assistantService.converse(job, priorEvents, sanitized);
            if (answer.isPresent() && !answer.get().routingHints().isEmpty()) {
                String withHints = appendCapped(
                        combined,
                        String.join(" ", answer.get().routingHints())
                );
                RepositoryMatch hintedMatch = repositoryMatcher.match(withHints);
                if (hintedMatch.status() == RepositoryMatch.Status.RESOLVED) {
                    combined = withHints;
                    match = hintedMatch;
                }
            }
        }
        String candidates = String.join(",", match.candidates());
        if (match.status() == RepositoryMatch.Status.RESOLVED) {
            job.applyRerouting(
                    combined,
                    match.repository(),
                    match.basis(),
                    match.confidence(),
                    candidates,
                    now
            );
            job.transitionTo(TaskStatus.PENDING, "context supplemented via conversation", now);
            eventRepository.save(new JobEvent(
                    job.getId(),
                    "STATUS_CHANGED",
                    status,
                    TaskStatus.PENDING,
                    ActorType.USER,
                    "context supplemented; rerouted to " + match.repository(),
                    now
            ));
            outboxService.schedule(job.getId(), now);
            return "已根据补充信息重新路由到授权仓库 " + match.repository()
                    + "（" + match.basis() + "），任务已重新排队。";
        }
        job.applyRerouting(combined, null, match.basis(), match.confidence(), candidates, now);
        job.transitionTo(status, match.basis(), now);
        RepositoryMatch unresolvedMatch = match;
        return answer
                .map(AssistantAnswer::reply)
                .orElseGet(() -> buildMissingContextReply(unresolvedMatch));
    }

    private static String appendCapped(String base, String extra) {
        String combined = (base + " " + extra).trim();
        return combined.length() > 4000 ? combined.substring(0, 4000) : combined;
    }

    private String buildMissingContextReply(RepositoryMatch match) {
        StringBuilder reply = new StringBuilder("信息仍不足以确定目标仓库（")
                .append(match.basis())
                .append("）。请补充该需求/故障所属的服务、模块或文件路径。授权仓库目录：");
        for (AppProperties.RepositoryDefinition definition : properties.repositoryCatalog()) {
            reply.append(" ")
                    .append(definition.repository())
                    .append("（关键词：")
                    .append(String.join("、", definition.keywords()))
                    .append("）");
        }
        if (reply.length() > 990) {
            reply.setLength(990);
            reply.append("…");
        }
        return reply.toString();
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

    @Transactional
    public void delete(String taskId) {
        AutomationJob job = findJobForUpdate(taskId);
        eventRepository.deleteByJobId(job.getId());
        outboxService.discard(job.getId());
        jobRepository.delete(job);
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
