package com.githubaiagent.controlplane.worker;

public class TaskQueueUnavailableException extends RuntimeException {
    public TaskQueueUnavailableException() {
        super("task queue is temporarily unavailable");
    }
}
