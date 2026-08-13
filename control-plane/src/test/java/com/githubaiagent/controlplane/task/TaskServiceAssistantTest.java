package com.githubaiagent.controlplane.task;

import com.githubaiagent.controlplane.assistant.AssistantAnswer;
import com.githubaiagent.controlplane.assistant.ChatGateway;
import com.githubaiagent.controlplane.assistant.ChatGatewayException;
import com.githubaiagent.controlplane.task.api.CreateTaskRequest;
import com.githubaiagent.controlplane.task.api.TaskDetailResponse;
import com.githubaiagent.controlplane.task.api.TaskEventResponse;
import com.githubaiagent.controlplane.task.api.TaskMessageRequest;
import com.githubaiagent.controlplane.task.api.TaskResponse;
import com.githubaiagent.controlplane.worker.TaskQueueOutboxRepository;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.transaction.annotation.Transactional;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyMap;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;

@SpringBootTest
@Transactional
class TaskServiceAssistantTest {

    @Autowired
    private TaskService taskService;

    @Autowired
    private TaskQueueOutboxRepository outboxRepository;

    @MockitoBean
    private ChatGateway chatGateway;

    @Test
    void modelRoutingHintsResolveAtCreationThroughTheDeterministicCatalogGate() {
        when(chatGateway.isActive()).thenReturn(true);
        when(chatGateway.converse(anyString(), anyMap())).thenReturn(new AssistantAnswer(
                "听起来是支付订单相关的问题，我已补充线索继续匹配。",
                List.of("支付", "订单"),
                List.of()
        ));

        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "修复一个接口报错"
        ));

        assertThat(created.status()).isEqualTo(TaskStatus.PENDING);
        assertThat(created.matchedRepository()).isEqualTo("demo-company/payment-service");
        TaskDetailResponse detail = taskService.detail(created.id());
        assertThat(detail.events()).extracting(TaskEventResponse::eventType)
                .containsExactly("TASK_CREATED", "AGENT_REPLY");
        assertThat(detail.events().getLast().detail()).contains("demo-company/payment-service");
        assertThat(outboxRepository.existsById(created.id())).isTrue();
    }

    @Test
    void modelRoutingHintsResolveOnFollowUpMessage() {
        when(chatGateway.isActive()).thenReturn(true);
        when(chatGateway.converse(anyString(), anyMap()))
                .thenReturn(new AssistantAnswer(
                        "还需要一点信息才能定位仓库。",
                        List.of(),
                        List.of("所属服务")
                ))
                .thenReturn(new AssistantAnswer(
                        "明白了，是支付订单链路的问题。",
                        List.of("支付", "订单"),
                        List.of()
                ));
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "修复一个接口报错"
        ));
        assertThat(created.status()).isEqualTo(TaskStatus.NEEDS_CONTEXT);

        TaskDetailResponse detail = taskService.postMessage(
                created.id(),
                new TaskMessageRequest("就是下单那个链路，具体仓库我说不上来")
        );

        assertThat(detail.task().status()).isEqualTo(TaskStatus.PENDING);
        assertThat(detail.task().matchedRepository()).isEqualTo("demo-company/payment-service");
        assertThat(detail.events()).extracting(TaskEventResponse::eventType)
                .contains("USER_MESSAGE", "STATUS_CHANGED", "AGENT_REPLY");
        assertThat(outboxRepository.existsById(created.id())).isTrue();
    }

    @Test
    void gatewayFailureFallsBackToDeterministicMissingContextReply() {
        when(chatGateway.isActive()).thenReturn(true);
        when(chatGateway.converse(anyString(), anyMap()))
                .thenThrow(new ChatGatewayException("AI gateway request failed"));
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "修复一个接口报错"
        ));

        TaskDetailResponse detail = taskService.postMessage(
                created.id(),
                new TaskMessageRequest("还不太清楚是哪里")
        );

        assertThat(detail.task().status()).isEqualTo(TaskStatus.NEEDS_CONTEXT);
        TaskEventResponse lastEvent = detail.events().getLast();
        assertThat(lastEvent.eventType()).isEqualTo("AGENT_REPLY");
        assertThat(lastEvent.detail()).contains("授权仓库目录");
        assertThat(outboxRepository.existsById(created.id())).isFalse();
    }

    @Test
    void blankModelReplyIsRejectedAndFallsBack() {
        when(chatGateway.isActive()).thenReturn(true);
        when(chatGateway.converse(anyString(), anyMap()))
                .thenReturn(new AssistantAnswer("   ", List.of(), List.of()));
        TaskResponse created = taskService.create(new CreateTaskRequest(
                SourceType.NATURAL_LANGUAGE,
                "修复一个接口报错"
        ));

        TaskDetailResponse detail = taskService.postMessage(
                created.id(),
                new TaskMessageRequest("再看看")
        );

        assertThat(detail.task().status()).isEqualTo(TaskStatus.NEEDS_CONTEXT);
        assertThat(detail.events().getLast().detail()).contains("授权仓库目录");
    }

    @Test
    void modelReplyIsUsedForCompletedTaskWithoutChangingStatus() {
        when(chatGateway.isActive()).thenReturn(true);
        when(chatGateway.converse(anyString(), anyMap())).thenReturn(new AssistantAnswer(
                "这个任务已经完成了，测试结果是通过的。",
                List.of(),
                List.of()
        ));
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
                new TaskMessageRequest("这个任务结果怎么样？")
        );

        assertThat(detail.task().status()).isEqualTo(TaskStatus.COMPLETED);
        assertThat(detail.events().getLast().detail()).contains("测试结果是通过的");
    }
}
