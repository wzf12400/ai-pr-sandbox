package com.githubaiagent.controlplane.task;

import com.githubaiagent.controlplane.assistant.AssistantAnswer;
import com.githubaiagent.controlplane.assistant.AssistantService;
import com.githubaiagent.controlplane.assistant.ChatProperties;
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
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.core.task.TaskExecutor;
import org.springframework.data.domain.PageRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;

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
    private final ChatProperties chatProperties;
    private final TaskExecutor assistantReplyExecutor;
    private final TransactionTemplate assistantTransaction;

    public TaskService(
            AutomationJobRepository jobRepository,
            JobEventRepository eventRepository,
            NaturalLanguageSanitizer sanitizer,
            RepositoryMatcher repositoryMatcher,
            AppProperties properties,
            TaskQueueOutboxService outboxService,
            AssistantService assistantService,
            ChatProperties chatProperties,
            @Qualifier("assistantReplyExecutor") TaskExecutor assistantReplyExecutor,
            PlatformTransactionManager transactionManager
    ) {
        this.jobRepository = jobRepository;
        this.eventRepository = eventRepository;
        this.sanitizer = sanitizer;
        this.repositoryMatcher = repositoryMatcher;
        this.properties = properties;
        this.outboxService = outboxService;
        this.assistantService = assistantService;
        this.chatProperties = chatProperties;
        this.assistantReplyExecutor = assistantReplyExecutor;
        this.assistantTransaction = new TransactionTemplate(transactionManager);
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
        } else {
            scheduleOpeningAssistantPass(job.getId());
        }
        // 同步模式（测试/直连执行器）下开场分析可能已就地完成重路由，
        // 重新读取托管实体以反映最新状态
        return TaskResponse.from(findJobForUpdate(job.getId()));
    }

    @Transactional
    public TaskDetailResponse postMessage(String taskId, TaskMessageRequest request) {
        String sanitized = sanitizer.sanitize(request.content());
        if (sanitized.isBlank()) {
            throw new IllegalArgumentException("message is empty after sanitization");
        }
        AutomationJob job = findJobForUpdate(taskId);
        TaskStatus status = job.getStatus();
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
            // 先跑确定性匹配：能直接解析就不调用 AI，同步完成重路由
            String combined = appendCapped(job.getNormalizedRequirement(), sanitized);
            RepositoryMatch match = repositoryMatcher.match(combined);
            if (match.status() == RepositoryMatch.Status.RESOLVED) {
                applyResolvedRerouting(job, combined, match, status, now);
                reply = "已根据补充信息重新路由到授权仓库 " + match.repository()
                        + "（" + match.basis() + "），任务已重新排队。";
                saveAgentReply(taskId, status, job.getStatus(), reply, now);
            } else {
                // 上下文先同步合并进需求，AI 线索分析异步补齐
                job.applyRerouting(
                        combined,
                        null,
                        match.basis(),
                        match.confidence(),
                        String.join(",", match.candidates()),
                        now
                );
                job.transitionTo(status, match.basis(), now);
                scheduleFollowUpAssistantPass(taskId, sanitized, status);
            }
        } else if (status == TaskStatus.COMPLETED) {
            scheduleAssistantReply(
                    taskId,
                    sanitized,
                    "该任务已完成。如有新的变更需求，请直接描述，我会创建新任务。"
            );
        } else {
            scheduleAssistantReply(
                    taskId,
                    sanitized,
                    "收到，补充信息已记录到事件流。任务当前处于「" + status
                            + "」状态，流水线处理中，不会被对话打断。"
            );
        }
        return detail(taskId);
    }

    private void applyResolvedRerouting(
            AutomationJob job,
            String requirement,
            RepositoryMatch match,
            TaskStatus fromStatus,
            Instant now
    ) {
        job.applyRerouting(
                requirement,
                match.repository(),
                match.basis(),
                match.confidence(),
                String.join(",", match.candidates()),
                now
        );
        job.transitionTo(TaskStatus.PENDING, "context supplemented via conversation", now);
        eventRepository.save(new JobEvent(
                job.getId(),
                "STATUS_CHANGED",
                fromStatus,
                TaskStatus.PENDING,
                ActorType.USER,
                "context supplemented; rerouted to " + match.repository(),
                now
        ));
        outboxService.schedule(job.getId(), now);
    }

    private void saveAgentReply(
            String taskId,
            TaskStatus fromStatus,
            TaskStatus toStatus,
            String reply,
            Instant now
    ) {
        eventRepository.save(new JobEvent(
                taskId,
                "AGENT_REPLY",
                fromStatus,
                toStatus,
                ActorType.ASSISTANT,
                reply,
                now
        ));
    }

    private void scheduleOpeningAssistantPass(String taskId) {
        submitAssistantWork(() -> assistantTransaction.executeWithoutResult(tx -> {
            AutomationJob job = findJobForUpdate(taskId);
            if (job.getStatus() != TaskStatus.NEEDS_CONTEXT) {
                return;
            }
            List<JobEvent> priorEvents =
                    eventRepository.findByJobIdOrderByCreatedAtAscIdAsc(taskId);
            Optional<AssistantAnswer> opening =
                    assistantService.converse(job, priorEvents, job.getInputSummary());
            RepositoryMatch match = repositoryMatcher.match(job.getNormalizedRequirement());
            if (opening.isPresent() && !opening.get().routingHints().isEmpty()) {
                String withHints = appendCapped(
                        job.getNormalizedRequirement(),
                        String.join(" ", opening.get().routingHints())
                );
                RepositoryMatch hintedMatch = repositoryMatcher.match(withHints);
                if (hintedMatch.status() == RepositoryMatch.Status.RESOLVED) {
                    Instant now = Instant.now();
                    applyResolvedRerouting(
                            job, withHints, hintedMatch, TaskStatus.NEEDS_CONTEXT, now);
                    saveAgentReply(
                            taskId,
                            TaskStatus.NEEDS_CONTEXT,
                            TaskStatus.PENDING,
                            "已根据你的描述路由到授权仓库 " + hintedMatch.repository()
                                    + "（" + hintedMatch.basis() + "），任务已排队。",
                            now
                    );
                    return;
                }
            }
            RepositoryMatch unresolvedMatch = match;
            String reply = opening
                    .map(AssistantAnswer::reply)
                    .orElseGet(() -> buildMissingContextReply(unresolvedMatch));
            saveAgentReply(
                    taskId, TaskStatus.NEEDS_CONTEXT, TaskStatus.NEEDS_CONTEXT,
                    reply, Instant.now());
        }));
    }

    private void scheduleFollowUpAssistantPass(
            String taskId,
            String sanitized,
            TaskStatus fromStatus
    ) {
        submitAssistantWork(() -> assistantTransaction.executeWithoutResult(tx -> {
            AutomationJob job = findJobForUpdate(taskId);
            if (job.getStatus() != fromStatus) {
                return;
            }
            List<JobEvent> priorEvents =
                    eventRepository.findByJobIdOrderByCreatedAtAscIdAsc(taskId);
            Optional<AssistantAnswer> answer =
                    assistantService.converse(job, priorEvents, sanitized);
            RepositoryMatch match = repositoryMatcher.match(job.getNormalizedRequirement());
            if (answer.isPresent() && !answer.get().routingHints().isEmpty()) {
                String withHints = appendCapped(
                        job.getNormalizedRequirement(),
                        String.join(" ", answer.get().routingHints())
                );
                RepositoryMatch hintedMatch = repositoryMatcher.match(withHints);
                if (hintedMatch.status() == RepositoryMatch.Status.RESOLVED) {
                    Instant now = Instant.now();
                    applyResolvedRerouting(job, withHints, hintedMatch, fromStatus, now);
                    saveAgentReply(
                            taskId,
                            fromStatus,
                            TaskStatus.PENDING,
                            "已根据补充信息重新路由到授权仓库 " + hintedMatch.repository()
                                    + "（" + hintedMatch.basis() + "），任务已重新排队。",
                            now
                    );
                    return;
                }
            }
            RepositoryMatch unresolvedMatch = match;
            String reply = answer
                    .map(AssistantAnswer::reply)
                    .orElseGet(() -> buildMissingContextReply(unresolvedMatch));
            saveAgentReply(taskId, fromStatus, fromStatus, reply, Instant.now());
        }));
    }

    private void scheduleAssistantReply(
            String taskId,
            String sanitized,
            String fallbackReply
    ) {
        submitAssistantWork(() -> assistantTransaction.executeWithoutResult(tx -> {
            AutomationJob job = findJobForUpdate(taskId);
            List<JobEvent> priorEvents =
                    eventRepository.findByJobIdOrderByCreatedAtAscIdAsc(taskId);
            String reply = assistantService.converse(job, priorEvents, sanitized)
                    .map(AssistantAnswer::reply)
                    .orElse(fallbackReply);
            saveAgentReply(taskId, job.getStatus(), job.getStatus(), reply, Instant.now());
        }));
    }

    private void submitAssistantWork(Runnable work) {
        // 测试配置（async-reply=false）下内联执行，保持断言同步
        if (!chatProperties.asyncReply()) {
            work.run();
            return;
        }
        // 请求事务提交后再执行，避免异步线程读到未提交数据或等待行锁
        if (TransactionSynchronizationManager.isSynchronizationActive()) {
            TransactionSynchronizationManager.registerSynchronization(
                    new TransactionSynchronization() {
                        @Override
                        public void afterCommit() {
                            assistantReplyExecutor.execute(work);
                        }
                    }
            );
        } else {
            assistantReplyExecutor.execute(work);
        }
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
