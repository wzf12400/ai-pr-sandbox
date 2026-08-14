package com.githubaiagent.controlplane.assistant;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.task.SyncTaskExecutor;
import org.springframework.core.task.TaskExecutor;
import org.springframework.scheduling.concurrent.ThreadPoolTaskExecutor;

/**
 * Executor for assistant (AI gateway) work. The AI round-trip is slow, so in
 * production replies are generated off the request thread and persisted after
 * the request transaction commits. Tests set app.chat.async-reply=false to run
 * everything inline and keep assertions synchronous.
 */
@Configuration
public class AssistantWorkConfig {

    @Bean
    public TaskExecutor assistantReplyExecutor(ChatProperties chatProperties) {
        if (!chatProperties.asyncReply()) {
            return new SyncTaskExecutor();
        }
        ThreadPoolTaskExecutor executor = new ThreadPoolTaskExecutor();
        // Single worker thread keeps per-task assistant replies in submission order.
        executor.setCorePoolSize(1);
        executor.setMaxPoolSize(1);
        executor.setQueueCapacity(50);
        executor.setThreadNamePrefix("assistant-reply-");
        executor.initialize();
        return executor;
    }
}
