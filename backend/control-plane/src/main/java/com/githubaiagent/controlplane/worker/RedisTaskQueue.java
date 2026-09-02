package com.githubaiagent.controlplane.worker;

import com.githubaiagent.controlplane.config.WorkerProperties;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.dao.DataAccessException;
import org.springframework.data.redis.connection.stream.MapRecord;
import org.springframework.data.redis.connection.stream.RecordId;
import org.springframework.data.redis.connection.stream.StreamRecords;
import org.springframework.data.redis.core.StringRedisTemplate;
import org.springframework.stereotype.Component;

import java.time.Instant;
import java.util.Map;

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
            MapRecord<String, String, String> message = StreamRecords
                    .newRecord()
                    .ofMap(Map.of(
                            "taskId", taskId,
                            "attempt", "0",
                            "enqueuedAt", Instant.now().toString(),
                            "schemaVersion", "1"
                    ))
                    .withStreamKey(properties.queueKey());
            RecordId recordId = redisTemplate.opsForStream().add(message);
            if (recordId == null) {
                LOGGER.warn("Redis did not return a stream record id for task {}", taskId);
                return false;
            }
            return true;
        } catch (DataAccessException exception) {
            LOGGER.warn("Could not publish task {} to the worker stream", taskId);
            return false;
        }
    }
}
