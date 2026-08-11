package com.githubaiagent.controlplane.worker;

import com.githubaiagent.controlplane.config.WorkerProperties;
import org.junit.jupiter.api.Test;
import org.springframework.data.domain.Pageable;

import java.time.Duration;
import java.time.Instant;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class TaskQueueOutboxPublisherTest {

    @Test
    void marksOutboxPublishedOnlyAfterRedisAcceptsTheMessage() {
        TaskQueueOutboxRepository repository = mock(TaskQueueOutboxRepository.class);
        TaskQueue taskQueue = mock(TaskQueue.class);
        TaskQueueOutbox outbox = new TaskQueueOutbox(
                "3f08ea61-71b4-42de-bc8e-608a18bba522",
                Instant.parse("2026-08-11T00:00:00Z")
        );
        when(repository.findPending(any(Pageable.class))).thenReturn(List.of(outbox));
        when(taskQueue.enqueue(outbox.getTaskId())).thenReturn(true);

        new TaskQueueOutboxPublisher(repository, taskQueue, properties()).publishPending();

        verify(taskQueue).enqueue(outbox.getTaskId());
        assertThat(outbox.getPublishedGeneration()).isEqualTo(outbox.getGeneration());
        assertThat(outbox.getPublishedAt()).isNotNull();
        assertThat(outbox.getLastError()).isNull();
    }

    @Test
    void leavesOutboxPendingWhenRedisIsUnavailable() {
        TaskQueueOutboxRepository repository = mock(TaskQueueOutboxRepository.class);
        TaskQueue taskQueue = mock(TaskQueue.class);
        TaskQueueOutbox outbox = new TaskQueueOutbox(
                "3f08ea61-71b4-42de-bc8e-608a18bba522",
                Instant.parse("2026-08-11T00:00:00Z")
        );
        when(repository.findPending(any(Pageable.class))).thenReturn(List.of(outbox));
        when(taskQueue.enqueue(outbox.getTaskId())).thenReturn(false);

        new TaskQueueOutboxPublisher(repository, taskQueue, properties()).publishPending();

        assertThat(outbox.getPublishedGeneration()).isZero();
        assertThat(outbox.getPublishAttemptCount()).isEqualTo(1);
        assertThat(outbox.getLastError()).isEqualTo("redis stream temporarily unavailable");
    }

    private static WorkerProperties properties() {
        return new WorkerProperties(
                true,
                "github-ai-agent:test-jobs:v2",
                100,
                Duration.ofSeconds(2),
                false,
                100,
                Duration.ofMinutes(5),
                Duration.ofMinutes(10),
                Duration.ofMinutes(30),
                3
        );
    }
}
