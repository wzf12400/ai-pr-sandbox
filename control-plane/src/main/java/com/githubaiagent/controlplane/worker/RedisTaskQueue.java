package com.githubaiagent.controlplane.worker;

import com.githubaiagent.controlplane.config.WorkerProperties;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.dao.DataAccessException;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

@Component
@ConditionalOnProperty(name = "app.worker.redis-enabled", havingValue = "true")
public class RedisTaskQueue implements TaskQueue {

    private static final Logger LOGGER = LoggerFactory.getLogger(RedisTaskQueue.class);

    private final StringRedisTemplate redisTemplate;
    private final WorkerProperties properties;

    public RedisTaskQueue(StringRedisTemplate redisTemplate, WorkerProperties properties) {
        this.redisTemplate = redisTemplate;
        this.properties = properties;
    }

    @Override
    public boolean enqueue(String taskId) {
        try {
            redisTemplate.opsForList().rightPush(properties.queueKey(), taskId);
            return true;
        } catch (DataAccessException exception) {
            LOGGER.warn("Could not enqueue task {} for the mock worker", taskId);
            return false;
        }
    }
}
