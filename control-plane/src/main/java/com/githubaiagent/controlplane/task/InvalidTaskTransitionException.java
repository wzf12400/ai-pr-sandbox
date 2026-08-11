package com.githubaiagent.controlplane.task;

public class InvalidTaskTransitionException extends RuntimeException {
    public InvalidTaskTransitionException(TaskStatus from, TaskStatus to) {
        super("task status cannot change from " + from + " to " + to);
    }
}
