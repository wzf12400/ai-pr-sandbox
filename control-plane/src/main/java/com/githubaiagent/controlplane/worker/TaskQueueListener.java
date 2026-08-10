package com.githubaiagent.controlplane.worker;

import org.springframework.stereotype.Component;
import org.springframework.transaction.event.TransactionPhase;
import org.springframework.transaction.event.TransactionalEventListener;

@Component
public class TaskQueueListener {

    private final TaskQueue taskQueue;

    public TaskQueueListener(TaskQueue taskQueue) {
        this.taskQueue = taskQueue;
    }

    @TransactionalEventListener(phase = TransactionPhase.AFTER_COMMIT)
    public void enqueueAfterCommit(TaskReadyForWorker event) {
        taskQueue.enqueue(event.taskId());
    }
}
