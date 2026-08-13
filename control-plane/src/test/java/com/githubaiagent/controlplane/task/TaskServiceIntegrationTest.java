package com.githubaiagent.controlplane.task;

import com.githubaiagent.controlplane.task.api.CreateTaskRequest;
import com.githubaiagent.controlplane.task.api.LogIncidentRequest;
import com.githubaiagent.controlplane.task.api.TaskClaimResponse;
import com.githubaiagent.controlplane.task.api.TaskDetailResponse;
import com.githubaiagent.controlplane.task.api.TaskEventResponse;
import com.githubaiagent.controlplane.task.api.TaskMessageRequest;
import com.githubaiagent.controlplane.task.api.TaskResponse;
import com.githubaiagent.controlplane.worker.TaskQueueOutbox;
import com.githubaiagent.controlplane.worker.TaskQueueOutboxRepository;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

@SpringBootTest
@Transactional
class TaskServiceIntegrationTest {

    @Autowired
    private TaskService taskService;

    @Autowired
    private AutomationJobRepository jobRepository;

    @Autowired
    private JobEventRepository eventRepository;

    @Autowired
    private TaskQueueOutboxRepository outboxRepository;

    @Test
    void createsPersistsAndTransitionsResolvedNaturalLanguageTask() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "支付订单列表最多显示 50 条，联系人 owner@example.com，并补充分页测试"
        ));

        assertThat(created.status()).isEqualTo(TaskStatus.PENDING);
        assertThat(created.matchedRepository()).isEqualTo("demo-company/payment-service");
        assertThat(created.inputSummary()).contains("[REDACTED_EMAIL]");
        assertThat(created.inputSummary()).doesNotContain("owner@example.com");

        TaskResponse processing = taskService.transition(
                created.id(),
                TaskStatus.PROCESSING,
                "mock worker claimed task",
                ActorType.MOCK_WORKER
        );
        TaskDetailResponse detail = taskService.detail(created.id());

        assertThat(processing.status()).isEqualTo(TaskStatus.PROCESSING);
        assertThat(detail.events()).hasSize(2);
        assertThat(detail.events().getFirst().eventType()).isEqualTo("TASK_CREATED");
        assertThat(detail.events().get(1).toStatus()).isEqualTo(TaskStatus.PROCESSING);
        assertThat(taskService.list()).extracting(TaskResponse::id).contains(created.id());
    }

    @Test
    void writesPendingTaskAndQueueOutboxInTheSameTransaction() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "支付订单列表增加分页测试"
        ));

        TaskQueueOutbox outbox = outboxRepository.findById(created.id()).orElseThrow();

        assertThat(created.status()).isEqualTo(TaskStatus.PENDING);
        assertThat(outbox.getGeneration()).isEqualTo(1);
        assertThat(outbox.getPublishedGeneration()).isZero();
        assertThat(outbox.getPublishedAt()).isNull();
    }

    @Test
    void manualRequeueDurablyAdvancesOutboxGeneration() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "支付订单列表增加分页测试"
        ));

        taskService.requeue(created.id());
        TaskQueueOutbox outbox = outboxRepository.findById(created.id()).orElseThrow();

        assertThat(outbox.getGeneration()).isEqualTo(2);
        assertThat(outbox.getPublishedGeneration()).isZero();
    }

    @Test
    void storesUncertainRoutingAsNeedsContextWithoutSelectingRepository() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "支付页面需要增加用户头像"
        ));

        assertThat(created.status()).isEqualTo(TaskStatus.NEEDS_CONTEXT);
        assertThat(created.matchedRepository()).isNull();
        assertThat(created.routingCandidates()).containsExactly(
                "demo-company/customer-portal",
                "demo-company/payment-service"
        );
        assertThat(created.blockedReason()).contains("ambiguous");
        assertThat(outboxRepository.existsById(created.id())).isFalse();
    }

    @Test
    void rejectsUnsafeStatusJump() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "支付订单列表增加分页测试"
        ));

        assertThatThrownBy(() -> taskService.transition(
                created.id(),
                TaskStatus.COMPLETED,
                "skip all gates",
                ActorType.MOCK_WORKER
        )).isInstanceOf(InvalidTaskTransitionException.class)
                .hasMessageContaining("PENDING to COMPLETED");
    }

    @Test
    void claimsPendingTaskOnceAndReturnsOnlyTheWorkerContract() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "支付订单列表增加分页测试"
        ));

        TaskClaimResponse claim = taskService.claim(created.id());

        assertThat(claim.taskId()).isEqualTo(created.id());
        assertThat(claim.executionMode()).isEqualTo(ExecutionMode.MOCK);
        assertThat(claim.normalizedRequirement()).contains("分页测试");
        assertThat(claim.matchedRepository()).isEqualTo("demo-company/payment-service");
        assertThat(taskService.detail(created.id()).task().status())
                .isEqualTo(TaskStatus.PROCESSING);
        assertThatThrownBy(() -> taskService.claim(created.id()))
                .isInstanceOf(com.githubaiagent.controlplane.worker.TaskClaimConflictException.class)
                .hasMessageContaining("PROCESSING");
    }

    @Test
    void mockWorkerCompletesSyntheticTestsWithoutCreatingAPullRequest() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "支付订单列表增加分页测试"
        ));
        taskService.claim(created.id());
        taskService.transition(
                created.id(),
                TaskStatus.TESTING,
                "synthetic tests started",
                ActorType.MOCK_WORKER
        );

        TaskResponse completed = taskService.transition(
                created.id(),
                TaskStatus.COMPLETED,
                "mock execution completed; no external systems were called",
                ActorType.MOCK_WORKER
        );
        TaskDetailResponse detail = taskService.detail(created.id());

        assertThat(completed.status()).isEqualTo(TaskStatus.COMPLETED);
        assertThat(completed.testSummary()).contains("no external systems");
        assertThat(completed.prNumber()).isNull();
        assertThat(completed.prUrl()).isNull();
        assertThat(detail.events()).hasSize(4);
        assertThat(detail.events()).extracting(event -> event.toStatus()).containsExactly(
                TaskStatus.PENDING,
                TaskStatus.PROCESSING,
                TaskStatus.TESTING,
                TaskStatus.COMPLETED
        );
    }

    @Test
    void recordsOneRepositoryBoundIssueReferenceIdempotently() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "支付订单列表增加分页测试"
        ));
        taskService.claim(created.id());
        String issueUrl = "https://github.com/demo-company/payment-service/issues/42";

        TaskResponse linked = taskService.attachIssue(created.id(), 42, issueUrl);
        TaskResponse repeated = taskService.attachIssue(created.id(), 42, issueUrl);
        TaskDetailResponse detail = taskService.detail(created.id());

        assertThat(linked.issueNumber()).isEqualTo(42);
        assertThat(linked.issueUrl()).isEqualTo(issueUrl);
        assertThat(repeated.issueUrl()).isEqualTo(issueUrl);
        assertThat(detail.events()).extracting(event -> event.eventType())
                .containsExactly("TASK_CREATED", "TASK_CLAIMED", "ISSUE_LINKED");
        assertThatThrownBy(() -> taskService.attachIssue(
                created.id(),
                43,
                "https://github.com/demo-company/payment-service/issues/43"
        )).isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("different Issue");
    }

    @Test
    void recordsTestedDraftPullRequestBeforeAwaitingHumanReview() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "支付订单列表增加分页测试"
        ));
        taskService.claim(created.id());
        taskService.attachIssue(
                created.id(),
                42,
                "https://github.com/demo-company/payment-service/issues/42"
        );
        taskService.transition(
                created.id(),
                TaskStatus.TESTING,
                "policy tests passed",
                ActorType.MOCK_WORKER
        );

        TaskResponse linked = taskService.attachPullRequest(
                created.id(),
                9,
                "https://github.com/demo-company/payment-service/pull/9",
                "策略测试 1 项通过"
        );
        TaskResponse awaiting = taskService.transition(
                created.id(),
                TaskStatus.AWAITING_PR_REVIEW,
                "Draft PR waits for review",
                ActorType.MOCK_WORKER
        );
        TaskDetailResponse detail = taskService.detail(created.id());

        assertThat(linked.prNumber()).isEqualTo(9);
        assertThat(linked.prUrl()).endsWith("/pull/9");
        assertThat(linked.testSummary()).contains("1 项通过");
        assertThat(awaiting.status()).isEqualTo(TaskStatus.AWAITING_PR_REVIEW);
        assertThat(detail.events()).extracting(event -> event.eventType())
                .contains("DRAFT_PR_LINKED");
    }

    @Test
    void refusesDirectMockCompletionByANonWorkerActor() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "支付订单列表增加分页测试"
        ));
        taskService.claim(created.id());
        taskService.transition(
                created.id(),
                TaskStatus.TESTING,
                "synthetic tests started",
                ActorType.MOCK_WORKER
        );

        assertThatThrownBy(() -> taskService.transition(
                created.id(),
                TaskStatus.COMPLETED,
                "incorrect actor",
                ActorType.GITHUB
        )).isInstanceOf(InvalidTaskTransitionException.class);
    }

    @Test
    void storesSanitizedLogIncidentWithExpandedOccurrenceFields() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.LOG,
                "支付服务 order 接口出现 NullPointerException",
                new LogIncidentRequest(
                        "SANITIZED",
                        "incident_ref:0123456789abcdefabcd",
                        Instant.parse("2026-08-04T01:00:00Z"),
                        Instant.parse("2026-08-04T02:00:00Z"),
                        5,
                        18,
                        2,
                        List.of("/api/orders"),
                        3,
                        7,
                        12,
                        true,
                        "service=payment; exception=NullPointerException"
                )
        ));

        assertThat(created.status()).isEqualTo(TaskStatus.PENDING);
        assertThat(created.issueProfile()).isEqualTo(IssueProfile.LOG_INCIDENT);
        assertThat(created.logIncident()).isNotNull();
        assertThat(created.logIncident().firstSeenAt())
                .isEqualTo(Instant.parse("2026-08-04T01:00:00Z"));
        assertThat(created.logIncident().historicalEventCount()).isEqualTo(18);
        assertThat(created.logIncident().affectedEndpoints())
                .containsExactly("/api/orders");

        TaskClaimResponse claim = taskService.claim(created.id());
        assertThat(claim.issueProfile()).isEqualTo(IssueProfile.LOG_INCIDENT);
        assertThat(claim.logIncident().currentScanEventCount()).isEqualTo(5);
    }

    @Test
    void reusesLogTaskWithTheSameStableSourceReference() {
        LogIncidentRequest incident = new LogIncidentRequest(
                "SANITIZED",
                "incident_ref:0123456789abcdefabcd",
                Instant.parse("2026-08-04T01:00:00Z"),
                Instant.parse("2026-08-04T02:00:00Z"),
                5,
                18,
                2,
                List.of("/api/orders"),
                3,
                7,
                12,
                true,
                "service=payment; exception=NullPointerException"
        );

        TaskResponse first = taskService.create(new CreateTaskRequest(
                SourceType.LOG,
                "支付服务 order 接口出现 NullPointerException",
                incident
        ));
        TaskResponse repeated = taskService.create(new CreateTaskRequest(
                SourceType.LOG,
                "支付服务 order 接口出现 NullPointerException",
                incident
        ));

        assertThat(repeated.id()).isEqualTo(first.id());
        assertThat(jobRepository.count()).isEqualTo(1);
        assertThat(eventRepository.findByJobIdOrderByCreatedAtAscIdAsc(first.id()))
                .hasSize(1);
    }

    @Test
    void rejectsRawOrIncompleteLogEvidence() {
        assertThatThrownBy(() -> taskService.create(new CreateTaskRequest(
                SourceType.LOG,
                "支付服务 token=not-sanitized",
                new LogIncidentRequest(
                        "RAW",
                        "incident_ref:0123456789abcdefabcd",
                        Instant.parse("2026-08-04T01:00:00Z"),
                        Instant.parse("2026-08-04T02:00:00Z"),
                        5,
                        18,
                        2,
                        List.of("/api/orders"),
                        null,
                        null,
                        0,
                        true,
                        "service=payment"
                )
        ))).isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("SANITIZED");
    }

    @Test
    void keepsJiraDisconnectedUntilSanitizedIntakeIsImplemented() {
        assertThatThrownBy(() -> taskService.create(new CreateTaskRequest(
                SourceType.JIRA,
                "synthetic Jira record"
        ))).isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("Jira");
    }

    @Test
    void supplementedContextReroutesNeedsContextTaskToPending() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "修复一个接口报错"
        ));
        assertThat(created.status()).isEqualTo(TaskStatus.NEEDS_CONTEXT);

        TaskDetailResponse detail = taskService.postMessage(
                created.id(),
                new TaskMessageRequest("这是支付订单模块的问题")
        );

        assertThat(detail.task().status()).isEqualTo(TaskStatus.PENDING);
        assertThat(detail.task().matchedRepository()).isEqualTo("demo-company/payment-service");
        assertThat(detail.task().normalizedRequirement()).contains("支付订单模块");
        assertThat(detail.events()).extracting(TaskEventResponse::eventType)
                .contains("USER_MESSAGE", "STATUS_CHANGED", "AGENT_REPLY");
        assertThat(outboxRepository.existsById(created.id())).isTrue();
    }

    @Test
    void insufficientContextKeepsNeedsContextAndAssistantExplainsWhatIsMissing() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "修复一个接口报错"
        ));

        TaskDetailResponse detail = taskService.postMessage(
                created.id(),
                new TaskMessageRequest("还不太清楚具体是哪里")
        );

        assertThat(detail.task().status()).isEqualTo(TaskStatus.NEEDS_CONTEXT);
        assertThat(detail.task().matchedRepository()).isNull();
        TaskEventResponse lastEvent = detail.events().getLast();
        assertThat(lastEvent.eventType()).isEqualTo("AGENT_REPLY");
        assertThat(lastEvent.actorType()).isEqualTo(ActorType.ASSISTANT);
        assertThat(lastEvent.detail()).contains("授权仓库目录");
        assertThat(outboxRepository.existsById(created.id())).isFalse();
    }

    @Test
    void messageOnCompletedTaskIsRecordedWithoutChangingStatus() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "支付订单列表增加分页测试"
        ));
        taskService.claim(created.id());
        taskService.transition(
                created.id(),
                TaskStatus.TESTING,
                "synthetic tests started",
                ActorType.MOCK_WORKER
        );
        taskService.transition(
                created.id(),
                TaskStatus.COMPLETED,
                "mock execution completed; no external systems were called",
                ActorType.MOCK_WORKER
        );

        TaskDetailResponse detail = taskService.postMessage(
                created.id(),
                new TaskMessageRequest("再改点别的")
        );

        assertThat(detail.task().status()).isEqualTo(TaskStatus.COMPLETED);
        assertThat(detail.events()).extracting(TaskEventResponse::eventType)
                .contains("USER_MESSAGE", "AGENT_REPLY");
    }

    @Test
    void rejectsBlankMessageAfterSanitization() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "支付订单列表增加分页测试"
        ));

        assertThatThrownBy(() -> taskService.postMessage(
                created.id(),
                new TaskMessageRequest("   ")
        )).isInstanceOf(IllegalArgumentException.class)
                .hasMessageContaining("empty");
    }

    @Test
    void deletesTaskWithEventsAndOutbox() {
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "支付订单列表增加分页测试"
        ));
        assertThat(outboxRepository.existsById(created.id())).isTrue();

        taskService.delete(created.id());

        assertThat(jobRepository.findById(created.id())).isEmpty();
        assertThat(eventRepository.findByJobIdOrderByCreatedAtAscIdAsc(created.id()))
                .isEmpty();
        assertThat(outboxRepository.existsById(created.id())).isFalse();
        assertThatThrownBy(() -> taskService.delete(created.id()))
                .isInstanceOf(TaskNotFoundException.class);
    }
}
