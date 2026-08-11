package com.githubaiagent.controlplane.task;

public class TaskNotFoundException extends RuntimeException {
    public TaskNotFoundException(String taskId) {
        super("task not found: " + taskId);
    }
}
