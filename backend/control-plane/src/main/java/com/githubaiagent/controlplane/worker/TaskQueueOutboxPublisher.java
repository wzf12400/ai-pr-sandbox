package com.githubaiagent.controlplane.worker;

import com.githubaiagent.controlplane.config.WorkerProperties;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.data.domain.PageRequest;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

import java.time.Instant;
import java.util.List;

@Component
@ConditionalOnProperty(name = "app.worker.redis-enabled", havingValue = "true")
public class TaskQueueOutboxPublisher {

    private final TaskQueueOutboxRepository repository;
    private final TaskQueue taskQueue;
    private final WorkerProperties properties;

    public TaskQueueOutboxPublisher(
            TaskQueueOutboxRepository repository,
            TaskQueue taskQueue,
            WorkerProperties properties
    ) {
        this.repository = repository;
        this.taskQueue = taskQueue;
        this.properties = properties;
    }

    @Scheduled(fixedDelayString = "${app.worker.outbox-publish-delay}")
    @Transactional
    public void publishPending() {
        List<TaskQueueOutbox> pending = repository.findPending(
                PageRequest.of(0, properties.outboxBatchSize())
        );
        for (TaskQueueOutbox outbox : pending) {
            Instant now = Instant.now();
            if (taskQueue.enqueue(outbox.getTaskId())) {
                outbox.markPublished(now);
            } else {
                outbox.markPublishFailure(now);
            }
        }
    }
}
