package com.githubaiagent.controlplane.worker;

import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

@Component
@ConditionalOnProperty(name = "app.worker.redis-enabled", havingValue = "false")
public class NoOpTaskQueue implements TaskQueue {

    @Override
    public boolean enqueue(String taskId) {
        return true;
    }
}
