package com.githubaiagent.controlplane.worker;

import com.githubaiagent.controlplane.task.TaskStatus;

public class TaskClaimConflictException extends RuntimeException {
    public TaskClaimConflictException(String taskId, TaskStatus status) {
        super("task " + taskId + " cannot be claimed from status " + status);
    }
}
