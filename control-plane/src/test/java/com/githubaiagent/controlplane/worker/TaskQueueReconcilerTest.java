package com.githubaiagent.controlplane.worker;

import com.githubaiagent.controlplane.config.WorkerProperties;
import com.githubaiagent.controlplane.task.AutomationJob;
import com.githubaiagent.controlplane.task.AutomationJobRepository;
import com.githubaiagent.controlplane.task.JobEventRepository;
import com.githubaiagent.controlplane.task.TaskStatus;
import org.junit.jupiter.api.Test;
import org.springframework.data.domain.Pageable;

import java.time.Duration;
import java.util.List;

import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;

class TaskQueueReconcilerTest {

    @Test
    void returnsSideEffectFreeStaleClaimToQueueAndBlocksAmbiguousTestingTask() {
        AutomationJobRepository jobRepository = mock(AutomationJobRepository.class);
        JobEventRepository eventRepository = mock(JobEventRepository.class);
        TaskQueueOutboxService outboxService = mock(TaskQueueOutboxService.class);
        AutomationJob stalePublished = mock(AutomationJob.class);
        AutomationJob processing = mock(AutomationJob.class);
        AutomationJob exhausted = mock(AutomationJob.class);
        AutomationJob testing = mock(AutomationJob.class);
        when(stalePublished.getId()).thenReturn("e6078a7b-e45e-43dd-84c6-3282b2487927");
        when(processing.getId()).thenReturn("3f08ea61-71b4-42de-bc8e-608a18bba522");
        when(processing.getIssueNumber()).thenReturn(null);
        when(exhausted.getId()).thenReturn("b173768e-ae59-4f57-aafd-84b8cd79acbe");
        when(exhausted.getIssueNumber()).thenReturn(null);
        when(exhausted.getRetryCount()).thenReturn(3);
        when(testing.getId()).thenReturn("7be2ded8-a716-4a3f-b606-63a5596ca701");
        when(jobRepository.findWithoutQueueOutbox(
                eq(TaskStatus.PENDING), any(Pageable.class)
        )).thenReturn(List.of());
        when(jobRepository.findWithStalePublishedQueueOutbox(
                eq(TaskStatus.PENDING), any(), any(Pageable.class)
        )).thenReturn(List.of(stalePublished));
        when(jobRepository.findAllByStatusAndUpdatedAtBeforeOrderByUpdatedAtAsc(
                eq(TaskStatus.PROCESSING), any(), any(Pageable.class)
        )).thenReturn(List.of(processing, exhausted));
        when(jobRepository.findAllByStatusAndUpdatedAtBeforeOrderByUpdatedAtAsc(
                eq(TaskStatus.TESTING), any(), any(Pageable.class)
        )).thenReturn(List.of(testing));

        new TaskQueueReconciler(
                jobRepository,
                eventRepository,
                outboxService,
                properties()
        ).recoverPendingTasksWithoutOutbox();

        verify(outboxService).schedule(eq(stalePublished.getId()), any());
        verify(outboxService).schedule(eq(processing.getId()), any());
        verify(processing).transitionTo(
                eq(TaskStatus.PENDING),
                eq("stale worker claim returned to the durable queue"),
                any()
        );
        verify(testing).transitionTo(
                eq(TaskStatus.NEEDS_CONTEXT),
                eq("stale worker state may include an external side effect; manual review required"),
                any()
        );
        verify(exhausted).transitionTo(
                eq(TaskStatus.NEEDS_CONTEXT),
                eq("stale worker retry limit reached; manual review required"),
                any()
        );
        verify(outboxService, never()).schedule(eq(testing.getId()), any());
        verify(outboxService, never()).schedule(eq(exhausted.getId()), any());
        verify(eventRepository, org.mockito.Mockito.times(3)).save(any());
    }

    private static WorkerProperties properties() {
        return new WorkerProperties(
                true,
                "github-ai-agent:test-jobs:v2",
                100,
                Duration.ofSeconds(2),
                true,
                100,
                Duration.ofMinutes(5),
                Duration.ofMinutes(10),
                Duration.ofMinutes(30),
                3
        );
    }
}
