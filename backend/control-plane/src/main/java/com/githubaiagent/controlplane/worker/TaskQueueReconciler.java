package com.githubaiagent.controlplane.worker;

import com.githubaiagent.controlplane.config.WorkerProperties;
import com.githubaiagent.controlplane.task.AutomationJob;
import com.githubaiagent.controlplane.task.AutomationJobRepository;
import com.githubaiagent.controlplane.task.ActorType;
import com.githubaiagent.controlplane.task.JobEvent;
import com.githubaiagent.controlplane.task.JobEventRepository;
import com.githubaiagent.controlplane.task.TaskStatus;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.data.domain.PageRequest;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.List;

@Component
@ConditionalOnProperty(name = "app.worker.reconciliation-enabled", havingValue = "true")
public class TaskQueueReconciler {

    private final AutomationJobRepository jobRepository;
    private final JobEventRepository eventRepository;
    private final TaskQueueOutboxService outboxService;
    private final WorkerProperties properties;

    public TaskQueueReconciler(
            AutomationJobRepository jobRepository,
            JobEventRepository eventRepository,
            TaskQueueOutboxService outboxService,
            WorkerProperties properties
    ) {
        this.jobRepository = jobRepository;
        this.eventRepository = eventRepository;
        this.outboxService = outboxService;
        this.properties = properties;
    }

    @Scheduled(fixedDelayString = "${app.worker.reconciliation-delay}")
    @Transactional
    public void recoverPendingTasksWithoutOutbox() {
        List<AutomationJob> pendingJobs = jobRepository.findWithoutQueueOutbox(
                TaskStatus.PENDING,
                PageRequest.of(0, properties.reconciliationBatchSize())
        );
        Instant now = Instant.now();
        for (AutomationJob job : pendingJobs) {
            outboxService.schedule(job.getId(), now);
        }
        List<AutomationJob> stalePublishedJobs = jobRepository
                .findWithStalePublishedQueueOutbox(
                        TaskStatus.PENDING,
                        now.minus(properties.reconciliationRepublishAfter()),
                        PageRequest.of(0, properties.reconciliationBatchSize())
                );
        for (AutomationJob job : stalePublishedJobs) {
            outboxService.schedule(job.getId(), now);
        }
        recoverStale(TaskStatus.PROCESSING, now);
        recoverStale(TaskStatus.TESTING, now);
    }

    private void recoverStale(TaskStatus status, Instant now) {
        Instant cutoff = now.minus(properties.staleTaskTimeout());
        List<AutomationJob> staleJobs = jobRepository
                .findAllByStatusAndUpdatedAtBeforeOrderByUpdatedAtAsc(
                        status,
                        cutoff,
                        PageRequest.of(0, properties.reconciliationBatchSize())
                );
        for (AutomationJob job : staleJobs) {
            TaskStatus targetStatus;
            String eventType;
            String detail;
            if (status == TaskStatus.PROCESSING
                    && job.getIssueNumber() == null
                    && job.getRetryCount() < properties.maxTaskRetries()) {
                targetStatus = TaskStatus.PENDING;
                eventType = "STALE_TASK_RECOVERED";
                detail = "stale worker claim returned to the durable queue";
                outboxService.schedule(job.getId(), now);
            } else {
                targetStatus = TaskStatus.NEEDS_CONTEXT;
                eventType = "STALE_TASK_BLOCKED";
                detail = status == TaskStatus.PROCESSING && job.getIssueNumber() == null
                        ? "stale worker retry limit reached; manual review required"
                        : "stale worker state may include an external side effect; manual review required";
            }
            job.transitionTo(targetStatus, detail, now);
            eventRepository.save(new JobEvent(
                    job.getId(),
                    eventType,
                    status,
                    targetStatus,
                    ActorType.SYSTEM,
                    detail,
                    now
            ));
        }
    }
}
