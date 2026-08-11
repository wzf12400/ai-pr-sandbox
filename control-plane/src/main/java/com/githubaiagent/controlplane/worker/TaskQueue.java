package com.githubaiagent.controlplane.worker;

public interface TaskQueue {
    boolean enqueue(String taskId);
}
