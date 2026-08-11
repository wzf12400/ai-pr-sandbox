package com.githubaiagent.controlplane.task;

import com.githubaiagent.controlplane.task.api.CreateTaskRequest;
import com.githubaiagent.controlplane.task.api.AttachIssueRequest;
import com.githubaiagent.controlplane.task.api.AttachPullRequestRequest;
import com.githubaiagent.controlplane.task.api.LogIncidentRequest;
import com.githubaiagent.controlplane.task.api.TaskClaimResponse;
import com.githubaiagent.controlplane.task.api.TaskDetailResponse;
import com.githubaiagent.controlplane.task.api.TaskResponse;
import com.githubaiagent.controlplane.task.api.TransitionTaskRequest;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.client.RestClient;

import java.time.Instant;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
class TaskApiIntegrationTest {

    @LocalServerPort
    private int port;

    @Autowired
    private JobEventRepository eventRepository;

    @Autowired
    private AutomationJobRepository jobRepository;

    @BeforeEach
    void clearDatabase() {
        eventRepository.deleteAll();
        jobRepository.deleteAll();
    }

    @Test
    void createsAndReadsTaskThroughHttpApi() {
        RestClient client = RestClient.create("http://127.0.0.1:" + port);

        ResponseEntity<TaskResponse> created = client.post()
                .uri("/api/tasks")
                .contentType(MediaType.APPLICATION_JSON)
                .body(new CreateTaskRequest(
                        SourceType.NATURAL_LANGUAGE,
                        "支付订单列表最多显示 50 条，并补充分页测试"
                ))
                .retrieve()
                .toEntity(TaskResponse.class);

        assertThat(created.getStatusCode()).isEqualTo(HttpStatus.CREATED);
        assertThat(created.getBody()).isNotNull();
        assertThat(created.getBody().matchedRepository())
                .isEqualTo("demo-company/payment-service");

        TaskDetailResponse detail = client.get()
                .uri("/api/tasks/{id}", created.getBody().id())
                .retrieve()
                .body(TaskDetailResponse.class);

        assertThat(detail).isNotNull();
        assertThat(detail.task().status()).isEqualTo(TaskStatus.PENDING);
        assertThat(detail.events()).hasSize(1);

        TaskClaimResponse claim = client.post()
                .uri("/api/internal/tasks/{id}/claim", created.getBody().id())
                .retrieve()
                .body(TaskClaimResponse.class);

        assertThat(claim).isNotNull();
        assertThat(claim.taskId()).isEqualTo(created.getBody().id());
        assertThat(claim.executionMode()).isEqualTo(ExecutionMode.MOCK);
        assertThat(claim.matchedRepository()).isEqualTo("demo-company/payment-service");

        String issueUrl = "https://github.com/demo-company/payment-service/issues/42";
        TaskResponse linked = client.post()
                .uri("/api/internal/tasks/{id}/issue", created.getBody().id())
                .contentType(MediaType.APPLICATION_JSON)
                .body(new AttachIssueRequest(42, issueUrl))
                .retrieve()
                .body(TaskResponse.class);

        assertThat(linked).isNotNull();
        assertThat(linked.issueNumber()).isEqualTo(42);
        assertThat(linked.issueUrl()).isEqualTo(issueUrl);

        TaskDetailResponse claimedDetail = client.get()
                .uri("/api/tasks/{id}", created.getBody().id())
                .retrieve()
                .body(TaskDetailResponse.class);

        assertThat(claimedDetail).isNotNull();
        assertThat(claimedDetail.task().status()).isEqualTo(TaskStatus.PROCESSING);
        assertThat(claimedDetail.events()).hasSize(3);
        assertThat(claimedDetail.events().getLast().eventType()).isEqualTo("ISSUE_LINKED");
    }

    @Test
    void createsExpandedLogIncidentThroughHttpApi() {
        RestClient client = RestClient.create("http://127.0.0.1:" + port);

        TaskResponse created = client.post()
                .uri("/api/tasks")
                .contentType(MediaType.APPLICATION_JSON)
                .body(new CreateTaskRequest(
                        SourceType.LOG,
                        "支付 order 接口出现 NullPointerException",
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
                ))
                .retrieve()
                .body(TaskResponse.class);

        assertThat(created).isNotNull();
        assertThat(created.issueProfile()).isEqualTo(IssueProfile.LOG_INCIDENT);
        assertThat(created.logIncident()).isNotNull();
        assertThat(created.logIncident().historicalEventCount()).isEqualTo(18);
        assertThat(created.logIncident().affectedUserCountMin()).isEqualTo(3);
        assertThat(created.logIncident().affectedUserCountMax()).isEqualTo(7);
    }

    @Test
    void recordsDraftPullRequestThroughInternalApi() {
        RestClient client = RestClient.create("http://127.0.0.1:" + port);
        TaskResponse created = client.post()
                .uri("/api/tasks")
                .contentType(MediaType.APPLICATION_JSON)
                .body(new CreateTaskRequest(
                        SourceType.NATURAL_LANGUAGE,
                        "支付订单列表增加分页测试"
                ))
                .retrieve()
                .body(TaskResponse.class);
        assertThat(created).isNotNull();
        client.post().uri("/api/internal/tasks/{id}/claim", created.id())
                .retrieve().toBodilessEntity();
        client.post().uri("/api/internal/tasks/{id}/issue", created.id())
                .contentType(MediaType.APPLICATION_JSON)
                .body(new AttachIssueRequest(
                        42,
                        "https://github.com/demo-company/payment-service/issues/42"
                ))
                .retrieve().toBodilessEntity();
        client.post().uri("/api/internal/tasks/{id}/transitions", created.id())
                .contentType(MediaType.APPLICATION_JSON)
                .body(new TransitionTaskRequest(TaskStatus.TESTING, "tests passed"))
                .retrieve().toBodilessEntity();

        TaskResponse linked = client.post()
                .uri("/api/internal/tasks/{id}/pull-request", created.id())
                .contentType(MediaType.APPLICATION_JSON)
                .body(new AttachPullRequestRequest(
                        9,
                        "https://github.com/demo-company/payment-service/pull/9",
                        "策略测试 1 项通过"
                ))
                .retrieve()
                .body(TaskResponse.class);

        assertThat(linked).isNotNull();
        assertThat(linked.prNumber()).isEqualTo(9);
        assertThat(linked.testSummary()).contains("1 项通过");
    }
}
