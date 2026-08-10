package com.githubaiagent.controlplane.config;

import jakarta.validation.constraints.NotBlank;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.validation.annotation.Validated;

@Validated
@ConfigurationProperties(prefix = "app.worker")
public record WorkerProperties(
        boolean redisEnabled,
        @NotBlank String queueKey
) {
}
