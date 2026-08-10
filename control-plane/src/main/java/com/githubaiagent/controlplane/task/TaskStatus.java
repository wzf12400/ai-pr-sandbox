package com.githubaiagent.controlplane.task;

public enum TaskStatus {
    PENDING,
    PROCESSING,
    TESTING,
    AWAITING_PR_REVIEW,
    COMPLETED,
    FAILED,
    NEEDS_CONTEXT
}
