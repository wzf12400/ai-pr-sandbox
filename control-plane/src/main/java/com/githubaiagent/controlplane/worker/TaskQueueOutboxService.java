package com.githubaiagent.controlplane.worker;

import org.springframework.stereotype.Service;

import java.time.Instant;

@Service
public class TaskQueueOutboxService {

    private final TaskQueueOutboxRepository repository;

    public TaskQueueOutboxService(TaskQueueOutboxRepository repository) {
        this.repository = repository;
    }

    public void schedule(String taskId, Instant now) {
        TaskQueueOutbox outbox = repository.findById(taskId).orElse(null);
        if (outbox == null) {
            repository.save(new TaskQueueOutbox(taskId, now));
        } else {
            outbox.reschedule(now);
        }
    }

}
