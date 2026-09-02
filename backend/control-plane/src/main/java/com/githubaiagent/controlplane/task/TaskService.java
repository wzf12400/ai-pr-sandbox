package com.githubaiagent.controlplane.task;

import com.githubaiagent.controlplane.assistant.AssistantAnswer;
import com.githubaiagent.controlplane.assistant.AssistantService;
import com.githubaiagent.controlplane.assistant.ChatProperties;
import com.githubaiagent.controlplane.config.AppProperties;
import com.githubaiagent.controlplane.routing.RepositoryMatch;
import com.githubaiagent.controlplane.routing.RepositoryMatcher;
import com.githubaiagent.controlplane.routing.LogRepositoryRouteService;
import com.githubaiagent.controlplane.task.api.CreateTaskRequest;
import com.githubaiagent.controlplane.task.api.CreateDependencyTaskRequest;
import com.githubaiagent.controlplane.task.api.JiraIssueRequest;
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
import org.springframework.dao.DataIntegrityViolationException;
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
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;

@Service
public class TaskService {

    private static final Map<TaskStatus, Set<TaskStatus>> ALLOWED_TRANSITIONS = allowedTransitions();
    private static final Set<String> WORKER_PROGRESS_STAGES = Set.of(
            "DRAFTING_ISSUE",
            "VALIDATING_ISSUE",
            "PUBLISHING_ISSUE",
            "ISSUE_READY",
            "PREPARING_CODE_CHANGE",
            "CODING_AND_TESTING",
            "CLOUD_AGENT_STATE",
            "VALIDATING_DRAFT_PR",
            "CROSS_REPO_DEPENDENCY_REJECTED"
    );
    private static final int MAX_LOG_INCIDENT_RETRIES = 3;
    private static final int MAX_DEPENDENCY_CHILDREN = 3;
    private static final int MAX_DEPENDENCY_DEPTH = 2;
    private static final Set<String> DEPENDENCY_REASON_CODES = Set.of(
            "API_CONTRACT_CHANGE",
            "CLIENT_COMPATIBILITY",
            "CROSS_REPO_IMPLEMENTATION",
            "SHARED_SCHEMA_CHANGE"
    );
    private static final Pattern SAFE_LOG_REFERENCE = Pattern.compile(
            "(?:incident_ref|event_ref):[0-9a-f]{16,64}"
    );
    private static final Pattern SAFE_JIRA_REFERENCE = Pattern.compile(
            "[A-Z][A-Z0-9_]{0,19}-\\d{1,7}"
    );
    private static final Pattern SAFE_JIRA_PROJECT_KEY = Pattern.compile(
            "[A-Z][A-Z0-9_]{0,19}"
    );
    private static final Pattern SAFE_ROUTE_ID = Pattern.compile(
            "[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    );
    private static final Pattern GITHUB_ISSUE_URL = Pattern.compile(
            "https://github\\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/\\d+"
    );

    private final AutomationJobRepository jobRepository;
    private final JobEventRepository eventRepository;
    private final NaturalLanguageSanitizer sanitizer;
    private final RepositoryMatcher repositoryMatcher;
    private final LogRepositoryRouteService logRouteService;
    private final AppProperties properties;
    private final TaskQueueOutboxService outboxService;
    private final AssistantService assistantService;
    private final ChatProperties chatProperties;
    private final TaskExecutor assistantReplyExecutor;
    private final TransactionTemplate taskTransaction;
    private final TransactionTemplate assistantTransaction;

    public TaskService(
            AutomationJobRepository jobRepository,
            JobEventRepository eventRepository,
            NaturalLanguageSanitizer sanitizer,
            RepositoryMatcher repositoryMatcher,
            LogRepositoryRouteService logRouteService,
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
        this.logRouteService = logRouteService;
        this.properties = properties;
        this.outboxService = outboxService;
        this.assistantService = assistantService;
        this.chatProperties = chatProperties;
        this.assistantReplyExecutor = assistantReplyExecutor;
        this.taskTransaction = new TransactionTemplate(transactionManager);
        this.assistantTransaction = new TransactionTemplate(transactionManager);
    }

    public TaskResponse create(CreateTaskRequest request) {
        String sourceReference = requestedSourceReference(request);
        try {
            TaskResponse created = taskTransaction.execute(
                    status -> createInTransaction(request)
            );
            if (created == null) {
                throw new IllegalStateException(
                        "task creation transaction returned no response"
                );
            }
            return created;
        } catch (DataIntegrityViolationException exception) {
            if (sourceReference == null) {
                throw exception;
            }
            TaskResponse existing = taskTransaction.execute(status -> jobRepository
                    .findFirstBySourceTypeAndSourceReferenceOrderByCreatedAtAsc(
                            request.sourceType(),
                            sourceReference
                    )
                    .map(TaskResponse::from)
                    .orElse(null));
            if (existing != null) {
                return existing;
            }
            throw exception;
        }
    }

    private TaskResponse createInTransaction(CreateTaskRequest request) {
        String sanitizedInput = sanitizer.sanitize(request.input());
        if (sanitizedInput.isBlank()) {
            throw new IllegalArgumentException("task input is empty after sanitization");
        }
        IssueProfile issueProfile = IssueProfile.NATURAL_LANGUAGE;
        LogIncidentRequest logIncident = null;
        JiraIssueRequest jiraIssue = null;
        RepositoryMatch logRouteMatch = null;
        if (request.sourceType() == SourceType.LOG) {
            logIncident = validateLogIncident(request, sanitizedInput);
            issueProfile = IssueProfile.LOG_INCIDENT;
            if (request.repositoryHint() != null) {
                throw new IllegalArgumentException(
                        "LOG repositoryHint is not allowed; use a database log route");
            }
            logRouteMatch = resolveLogRoute(logIncident.routeId());
            var existingLogTask = jobRepository
                    .findFirstBySourceTypeAndSourceReferenceOrderByCreatedAtAsc(
                            SourceType.LOG,
                            logIncident.sourceReference()
                    );
            if (existingLogTask.isPresent()) {
                var existing = existingLogTask.get();
                if (existing.getStatus() == TaskStatus.NEEDS_CONTEXT
                        && logRouteMatch.status() == RepositoryMatch.Status.RESOLVED) {
                    existing.bindLogRoute(logIncident.routeId(), Instant.now());
                    applyResolvedRerouting(
                            existing,
                            existing.getNormalizedRequirement(),
                            logRouteMatch,
                            TaskStatus.NEEDS_CONTEXT,
                            Instant.now()
                    );
                    return TaskResponse.from(existing);
                }
                // 自愈：上次执行失败（如 worker 临时故障）且重试次数未超限的
                // 任务，监控再次超阈值时自动重新排队，避免永久 FAILED
                if (existing.getStatus() == TaskStatus.FAILED
                        && existing.getRetryCount() < MAX_LOG_INCIDENT_RETRIES
                        && logRouteMatch.status() == RepositoryMatch.Status.RESOLVED) {
                    existing.bindLogRoute(logIncident.routeId(), Instant.now());
                    applyResolvedRerouting(
                            existing,
                            existing.getNormalizedRequirement(),
                            logRouteMatch,
                            TaskStatus.FAILED,
                            Instant.now()
                    );
                    return TaskResponse.from(existing);
                }
                return TaskResponse.from(existingLogTask.get());
            }
        } else if (request.logIncident() != null) {
            throw new IllegalArgumentException(
                    "logIncident is allowed only when sourceType is LOG"
            );
        }
        if (request.sourceType() == SourceType.JIRA) {
            jiraIssue = validateJiraIssue(request, sanitizedInput);
            issueProfile = IssueProfile.JIRA_ISSUE;
            var existingJiraTask = jobRepository
                    .findFirstBySourceTypeAndSourceReferenceOrderByCreatedAtAsc(
                            SourceType.JIRA,
                            jiraIssue.sourceReference()
                    );
            if (existingJiraTask.isPresent()) {
                return TaskResponse.from(existingJiraTask.get());
            }
        } else if (request.jiraIssue() != null) {
            throw new IllegalArgumentException(
                    "jiraIssue is allowed only when sourceType is JIRA"
            );
        }

        RepositoryMatch match;
        if (jiraIssue != null) {
            match = new RepositoryMatch(
                    RepositoryMatch.Status.RESOLVED,
                    jiraIssue.resolvedRepository(),
                    "jira deterministic mapping: " + jiraIssue.mappingBasis(),
                    100,
                    List.of(jiraIssue.resolvedRepository())
            );
        } else if (logIncident != null) {
            match = logRouteMatch;
        } else if (request.repositoryHint() != null
                && repositoryMatcher.isAuthorized(request.repositoryHint())) {
            match = new RepositoryMatch(
                    RepositoryMatch.Status.RESOLVED,
                    request.repositoryHint(),
                    "evidence-based repository hint from source monitor",
                    100,
                    List.of(request.repositoryHint())
            );
        } else {
            match = repositoryMatcher.match(sanitizedInput);
        }
        String requirement = sanitizedInput;
        String sourceReference = logIncident != null
                ? logIncident.sourceReference()
                : jiraIssue == null ? null : jiraIssue.sourceReference();
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
                sourceReference,
                logIncident == null ? null : logIncident.routeId(),
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
            String combined = appendCapped(job.getNormalizedRequirement(), sanitized);
            restoreRepositoryFromIssue(job, combined, now);
            if (job.getMatchedRepository() != null) {
                releaseLegacyCcaReservation(job, status, now);
                boolean freshAgentAttempt = resetRejectedAgentTaskForRetry(
                        job, status, now);
                job.applyRerouting(
                        combined,
                        job.getMatchedRepository(),
                        job.getRoutingBasis(),
                        job.getRoutingConfidence(),
                        job.getRoutingCandidates(),
                        now
                );
                requeuePreservingRoute(job, status, now);
                reply = freshAgentAttempt
                        ? "已保留原仓库 " + job.getMatchedRepository()
                                + " 和已有 Issue，并开启新的 Cloud Agent 执行。"
                        : "已保留原仓库 " + job.getMatchedRepository()
                                + " 和已有 Issue，任务已重新排队。";
                saveAgentReply(taskId, status, job.getStatus(), reply, now);
            } else {
                // 先跑确定性匹配：能直接解析就不调用 AI，同步完成重路由
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

    private void restoreRepositoryFromIssue(
            AutomationJob job,
            String requirement,
            Instant now
    ) {
        if (job.getMatchedRepository() != null || job.getIssueUrl() == null) {
            return;
        }
        var matcher = GITHUB_ISSUE_URL.matcher(job.getIssueUrl());
        if (!matcher.matches()) {
            return;
        }
        String repository = matcher.group(1);
        boolean authorized = properties.repositoryCatalog().stream()
                .anyMatch(definition -> definition.repository().equals(repository));
        if (!authorized) {
            return;
        }
        job.applyRerouting(
                requirement,
                repository,
                "recovered from existing GitHub Issue reference",
                100,
                repository,
                now
        );
    }

    private void releaseLegacyCcaReservation(
            AutomationJob job,
            TaskStatus status,
            Instant now
    ) {
        boolean confirmedCcaRejection = eventRepository
                .findByJobIdOrderByCreatedAtAscIdAsc(job.getId())
                .stream()
                .map(JobEvent::getDetail)
                .filter(Objects::nonNull)
                .anyMatch(detail -> detail.contains(
                        "HTTP 409: user or repo does not have CCA enabled"
                ));
        if ((status != TaskStatus.FAILED && status != TaskStatus.NEEDS_CONTEXT)
                || job.getAgentTaskId() != null
                || job.getAgentTaskUrl() != null
                || job.getAgentSubmissionKey() == null
                || !confirmedCcaRejection) {
            return;
        }
        if (job.releaseAgentTaskReservation(now)) {
            eventRepository.save(new JobEvent(
                    job.getId(),
                    "CLOUD_AGENT_SUBMISSION_RELEASED",
                    status,
                    status,
                    ActorType.USER,
                    "legacy CCA-disabled rejection confirmed no remote task was created",
                    now
            ));
        }
    }

    private boolean resetRejectedAgentTaskForRetry(
            AutomationJob job,
            TaskStatus status,
            Instant now
    ) {
        String blockedReason = job.getBlockedReason();
        if ((status != TaskStatus.FAILED && status != TaskStatus.NEEDS_CONTEXT)
                || blockedReason == null
                || job.getAgentTaskId() == null) {
            return false;
        }
        boolean retryableAgentResult =
                blockedReason.contains("Draft PR 没有产生代码变更")
                || blockedReason.contains("Pull Request exceeds policy limits")
                || blockedReason.contains("Pull Request path is not allowed")
                || blockedReason.contains(
                        "resumed cloud-agent Issue snapshot or policy no longer matches");
        if (!retryableAgentResult) {
            return false;
        }
        String completedAgentTaskId = job.getAgentTaskId();
        if (!job.resetCompletedAgentTaskForRetry(now)) {
            return false;
        }
        eventRepository.save(new JobEvent(
                job.getId(),
                "CLOUD_AGENT_ATTEMPT_RESET",
                status,
                status,
                ActorType.USER,
                "completed cloud-agent task " + completedAgentTaskId
                        + " cannot be reused under the current policy; "
                        + "user requested a fresh attempt",
                now
        ));
        return true;
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

    private void requeuePreservingRoute(
            AutomationJob job,
            TaskStatus fromStatus,
            Instant now
    ) {
        job.transitionTo(TaskStatus.PENDING, "user requested retry", now);
        eventRepository.save(new JobEvent(
                job.getId(),
                "STATUS_CHANGED",
                fromStatus,
                TaskStatus.PENDING,
                ActorType.USER,
                "user requested retry; preserved repository binding",
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
        if (!sanitizedInput.equals(sanitizer.normalizeWhitespace(request.input()))) {
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

    private RepositoryMatch resolveLogRoute(String routeId) {
        if (routeId == null || !SAFE_ROUTE_ID.matcher(routeId).matches()) {
            throw new IllegalArgumentException("LOG routeId is invalid");
        }
        return logRouteService.resolveEnabled(routeId)
                .map(route -> new RepositoryMatch(
                        RepositoryMatch.Status.RESOLVED,
                        route.getRepository(),
                        "database log route: "
                                + route.getSelectorField()
                                + "="
                                + route.getSelectorValue(),
                        100,
                        List.of(route.getRepository())
                ))
                .orElseGet(() -> new RepositoryMatch(
                        RepositoryMatch.Status.NEEDS_CONTEXT,
                        null,
                        "no enabled authorized database log route matched routeId",
                        0,
                        List.of()
                ));
    }

    private JiraIssueRequest validateJiraIssue(
            CreateTaskRequest request,
            String sanitizedInput
    ) {
        JiraIssueRequest evidence = request.jiraIssue();
        if (evidence == null) {
            throw new IllegalArgumentException("JIRA tasks require jiraIssue evidence");
        }
        if (!"SANITIZED".equals(evidence.dataSafetyStatus())) {
            throw new IllegalArgumentException(
                    "JIRA tasks accept only SANITIZED issue evidence"
            );
        }
        if (!sanitizedInput.equals(sanitizer.normalizeWhitespace(request.input()))) {
            throw new IllegalArgumentException(
                    "JIRA issue summary still contains content requiring redaction"
            );
        }
        if (!SAFE_JIRA_REFERENCE.matcher(evidence.sourceReference()).matches()) {
            throw new IllegalArgumentException("JIRA sourceReference is invalid");
        }
        if (!SAFE_JIRA_PROJECT_KEY.matcher(evidence.projectKey()).matches()) {
            throw new IllegalArgumentException("JIRA projectKey is invalid");
        }
        if (!evidence.sourceReference().startsWith(evidence.projectKey() + "-")) {
            throw new IllegalArgumentException(
                    "JIRA sourceReference does not belong to projectKey"
            );
        }
        if (!evidence.issueUrl().startsWith("https://")) {
            throw new IllegalArgumentException("JIRA issueUrl must use https");
        }
        boolean authorized = properties.repositoryCatalog().stream()
                .anyMatch(definition -> definition.repository().equals(evidence.resolvedRepository()));
        if (!authorized) {
            throw new IllegalArgumentException(
                    "JIRA resolvedRepository is not in the authorized catalog"
            );
        }
        if (!sanitizer.sanitize(evidence.mappingBasis())
                .equals(evidence.mappingBasis().trim())) {
            throw new IllegalArgumentException(
                    "mappingBasis still contains content requiring redaction"
            );
        }
        return evidence;
    }

    @Transactional(readOnly = true)
    public List<TaskResponse> list(SourceType sourceType) {
        List<AutomationJob> jobs = sourceType == null
                ? jobRepository.findAllByOrderByCreatedAtDesc(PageRequest.of(0, 100))
                : jobRepository.findAllBySourceTypeOrderByCreatedAtDesc(
                        sourceType,
                        PageRequest.of(0, 100)
                );
        return jobs.stream()
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
        TaskResponse parentTask = job.getParentTaskId() == null
                ? null
                : jobRepository.findById(job.getParentTaskId())
                        .map(TaskResponse::from)
                        .orElse(null);
        List<TaskResponse> childTasks =
                jobRepository.findAllByParentTaskIdOrderByCreatedAtAsc(taskId).stream()
                        .map(TaskResponse::from)
                        .toList();
        return new TaskDetailResponse(TaskResponse.from(job), events, parentTask, childTasks);
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

    private String requestedSourceReference(CreateTaskRequest request) {
        if (request.sourceType() == SourceType.LOG
                && request.logIncident() != null) {
            return request.logIncident().sourceReference();
        }
        if (request.sourceType() == SourceType.JIRA
                && request.jiraIssue() != null) {
            return request.jiraIssue().sourceReference();
        }
        return null;
    }

    @Transactional
    public void recordProgress(String taskId, String stage, String detail) {
        if (!WORKER_PROGRESS_STAGES.contains(stage)) {
            throw new IllegalArgumentException("worker progress stage is not allowed");
        }
        AutomationJob job = findJobForUpdate(taskId);
        TaskStatus status = job.getStatus();
        if (status != TaskStatus.PROCESSING && status != TaskStatus.TESTING) {
            throw new IllegalArgumentException(
                    "worker progress is allowed only while task is PROCESSING or TESTING"
            );
        }
        String sanitized = sanitizer.sanitize(detail);
        if (sanitized.isBlank()) {
            throw new IllegalArgumentException("worker progress detail is empty after sanitization");
        }
        eventRepository.save(new JobEvent(
                taskId,
                stage,
                status,
                status,
                ActorType.MOCK_WORKER,
                sanitized,
                Instant.now()
        ));
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
    public TaskResponse attachAgentTask(
            String taskId,
            String agentTaskId,
            String agentTaskUrl
    ) {
        AutomationJob job = findJobForUpdate(taskId);
        if (job.getStatus() != TaskStatus.PROCESSING) {
            throw new IllegalArgumentException(
                    "cloud-agent task may only be attached while task is PROCESSING"
            );
        }
        if (job.getIssueNumber() == null || job.getIssueUrl() == null) {
            throw new IllegalArgumentException(
                    "cloud-agent task requires a recorded GitHub Issue"
            );
        }
        if (!agentTaskId.matches("[A-Za-z0-9_-]{1,160}")) {
            throw new IllegalArgumentException("cloud-agent task ID is invalid");
        }
        if (!agentTaskUrl.startsWith("https://")) {
            throw new IllegalArgumentException("cloud-agent task URL must use https");
        }
        Instant now = Instant.now();
        if (job.attachAgentTask(agentTaskId, agentTaskUrl, now)) {
            eventRepository.save(new JobEvent(
                    taskId,
                    "CLOUD_AGENT_TASK_LINKED",
                    TaskStatus.PROCESSING,
                    TaskStatus.PROCESSING,
                    ActorType.MOCK_WORKER,
                    "GitHub Copilot cloud-agent task reference recorded",
                    now
            ));
        }
        return TaskResponse.from(job);
    }

    @Transactional
    public TaskResponse reserveAgentTask(
            String taskId,
            String submissionKey,
            String issueSha256,
            String policySha256
    ) {
        AutomationJob job = findJobForUpdate(taskId);
        if (job.getStatus() != TaskStatus.PROCESSING) {
            throw new IllegalArgumentException(
                    "cloud-agent submission may only be reserved while task is PROCESSING"
            );
        }
        if (job.getIssueNumber() == null || job.getIssueUrl() == null) {
            throw new IllegalArgumentException(
                    "cloud-agent submission requires a recorded GitHub Issue"
            );
        }
        Pattern sha256 = Pattern.compile("[0-9a-f]{64}");
        if (!sha256.matcher(submissionKey).matches()
                || !sha256.matcher(issueSha256).matches()
                || !sha256.matcher(policySha256).matches()) {
            throw new IllegalArgumentException(
                    "cloud-agent submission reservation digest is invalid"
            );
        }
        Instant now = Instant.now();
        if (job.reserveAgentTask(submissionKey, issueSha256, policySha256, now)) {
            eventRepository.save(new JobEvent(
                    taskId,
                    "CLOUD_AGENT_SUBMISSION_RESERVED",
                    TaskStatus.PROCESSING,
                    TaskStatus.PROCESSING,
                    ActorType.MOCK_WORKER,
                    "exact Issue snapshot and code policy reserved before remote submission",
                    now
            ));
        }
        return TaskResponse.from(job);
    }

    @Transactional
    public TaskResponse releaseAgentTaskReservation(String taskId) {
        AutomationJob job = findJobForUpdate(taskId);
        if (job.getStatus() != TaskStatus.PROCESSING) {
            throw new IllegalArgumentException(
                    "cloud-agent submission reservation may only be released while task is PROCESSING"
            );
        }
        Instant now = Instant.now();
        if (job.releaseAgentTaskReservation(now)) {
            eventRepository.save(new JobEvent(
                    taskId,
                    "CLOUD_AGENT_SUBMISSION_RELEASED",
                    TaskStatus.PROCESSING,
                    TaskStatus.PROCESSING,
                    ActorType.MOCK_WORKER,
                    "GitHub confirmed no cloud-agent task was created",
                    now
            ));
        }
        return TaskResponse.from(job);
    }

    @Transactional
    public void heartbeat(String taskId) {
        AutomationJob job = findJobForUpdate(taskId);
        if (job.getStatus() != TaskStatus.PROCESSING) {
            throw new IllegalArgumentException(
                    "worker heartbeat is allowed only while task is PROCESSING"
            );
        }
        job.heartbeat(Instant.now());
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
                    "Draft PR may only be attached after remote validation enters TESTING"
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
                    "validated Draft PR reference recorded",
                    now
            ));
        }
        return TaskResponse.from(job);
    }

    @Transactional
    public List<TaskResponse> createDependencyTasks(
            String parentTaskId,
            List<CreateDependencyTaskRequest> requests
    ) {
        AutomationJob parent = findJobForUpdate(parentTaskId);
        if (parent.getStatus() != TaskStatus.TESTING
                && parent.getStatus() != TaskStatus.AWAITING_PR_REVIEW) {
            throw new IllegalArgumentException(
                    "cross-repository dependency requires a validated Draft PR"
            );
        }
        if (parent.getPrNumber() == null || parent.getPrUrl() == null) {
            throw new IllegalArgumentException(
                    "cross-repository dependency requires a recorded Draft PR"
            );
        }
        if (requests == null || requests.isEmpty()
                || requests.size() > MAX_DEPENDENCY_CHILDREN) {
            throw new IllegalArgumentException(
                    "cross-repository dependency batch size is invalid"
            );
        }
        String sourceRepository = parent.getMatchedRepository();
        Set<String> targetRepositories = requests.stream()
                .map(CreateDependencyTaskRequest::targetRepository)
                .collect(java.util.stream.Collectors.toSet());
        if (targetRepositories.size() != requests.size()) {
            throw new IllegalArgumentException(
                    "cross-repository dependency batch contains duplicate repositories"
            );
        }
        long existingChildren = jobRepository.countByParentTaskId(parentTaskId);
        long newChildren = targetRepositories.stream()
                .filter(target -> jobRepository
                        .findFirstByParentTaskIdAndMatchedRepository(parentTaskId, target)
                        .isEmpty())
                .count();
        if (existingChildren + newChildren > MAX_DEPENDENCY_CHILDREN) {
            throw new IllegalArgumentException(
                    "cross-repository dependency child limit exceeded"
            );
        }

        List<ValidatedDependency> validated = requests.stream()
                .map(request -> validateDependency(
                        parent, sourceRepository, request))
                .toList();
        return validated.stream()
                .map(item -> persistDependency(parent, item))
                .map(TaskResponse::from)
                .toList();
    }

    private ValidatedDependency validateDependency(
            AutomationJob parent,
            String sourceRepository,
            CreateDependencyTaskRequest request
    ) {
        String targetRepository = request.targetRepository();
        if (!repositoryMatcher.isAuthorized(targetRepository)
                || !repositoryMatcher.isAuthorizedDependency(
                        sourceRepository, targetRepository)) {
            throw new IllegalArgumentException(
                    "cross-repository dependency is not authorized by the repository catalog"
            );
        }
        if (!DEPENDENCY_REASON_CODES.contains(request.reasonCode())) {
            throw new IllegalArgumentException(
                    "cross-repository dependency reason code is not allowed"
            );
        }
        validateDependencyChain(parent, targetRepository);
        String summary = sanitizer.sanitize(request.summary());
        if (summary.isBlank()) {
            throw new IllegalArgumentException(
                    "cross-repository dependency summary is empty after sanitization"
            );
        }
        Optional<AutomationJob> existing =
                jobRepository.findFirstByParentTaskIdAndMatchedRepository(
                        parent.getId(), targetRepository);
        if (existing.isPresent()) {
            AutomationJob child = existing.get();
            if (!request.reasonCode().equals(child.getDependencyReasonCode())
                    || !summary.equals(child.getDependencySummary())) {
                throw new IllegalArgumentException(
                        "cross-repository dependency conflicts with the existing child task"
                );
            }
        }
        return new ValidatedDependency(
                targetRepository, request.reasonCode(), summary, existing.orElse(null));
    }

    private AutomationJob persistDependency(
            AutomationJob parent,
            ValidatedDependency dependency
    ) {
        if (dependency.existing() != null) {
            return dependency.existing();
        }
        Instant now = Instant.now();
        String inputSummary = appendCapped(
                "跨仓依赖 " + dependency.reasonCode() + "：" + dependency.summary(),
                "来源 Draft PR：" + parent.getPrUrl()
        );
        String requirement = appendCapped(
                inputSummary,
                "上游仓库：" + parent.getMatchedRepository()
                        + "。原始需求：" + parent.getNormalizedRequirement()
        );
        AutomationJob child = new AutomationJob(
                UUID.randomUUID().toString(),
                SourceType.NATURAL_LANGUAGE,
                ExecutionMode.MOCK,
                IssueProfile.NATURAL_LANGUAGE,
                inputSummary,
                requirement,
                null, null, null, null, null, null, null, null, null, null, null, null, null,
                TaskStatus.PENDING,
                dependency.targetRepository(),
                "validated cross-repository dependency from " + parent.getMatchedRepository(),
                100,
                dependency.targetRepository(),
                parent.getSubmittedBy(),
                parent.getPolicyId(),
                null,
                now
        );
        child.attachDependencyParent(
                parent.getId(), dependency.reasonCode(), dependency.summary(), now);
        jobRepository.save(child);
        eventRepository.save(new JobEvent(
                child.getId(), "TASK_CREATED", null, TaskStatus.PENDING, ActorType.SYSTEM,
                "由父任务 " + parent.getId() + " 的跨仓依赖创建", now
        ));
        eventRepository.save(new JobEvent(
                parent.getId(), "CROSS_REPO_DEPENDENCY_CREATED",
                parent.getStatus(), parent.getStatus(), ActorType.MOCK_WORKER,
                "已创建关联任务 " + child.getId()
                        + "，目标仓库 " + dependency.targetRepository(), now
        ));
        outboxService.schedule(child.getId(), now);
        return child;
    }

    private record ValidatedDependency(
            String targetRepository,
            String reasonCode,
            String summary,
            AutomationJob existing
    ) {
    }

    private void validateDependencyChain(
            AutomationJob parent,
            String targetRepository
    ) {
        AutomationJob cursor = parent;
        int depth = 0;
        while (true) {
            if (targetRepository.equals(cursor.getMatchedRepository())) {
                throw new IllegalArgumentException(
                        "cross-repository dependency would create a repository cycle"
                );
            }
            if (cursor.getParentTaskId() == null) {
                return;
            }
            depth++;
            if (depth >= MAX_DEPENDENCY_DEPTH) {
                throw new IllegalArgumentException(
                        "cross-repository dependency depth limit exceeded"
                );
            }
            cursor = jobRepository.findById(cursor.getParentTaskId())
                    .orElseThrow(() -> new IllegalArgumentException(
                            "cross-repository dependency parent chain is incomplete"
                    ));
        }
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
