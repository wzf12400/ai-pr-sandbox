package com.githubaiagent.controlplane.config;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotNull;
import jakarta.validation.constraints.Positive;
import jakarta.validation.constraints.PositiveOrZero;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.validation.annotation.Validated;

import java.time.Duration;

@Validated
@ConfigurationProperties(prefix = "app.worker")
public record WorkerProperties(
        boolean redisEnabled,
        @NotBlank String queueKey,
        @Positive int outboxBatchSize,
        @NotNull Duration outboxPublishDelay,
        boolean reconciliationEnabled,
        @Positive int reconciliationBatchSize,
        @NotNull Duration reconciliationDelay,
        @NotNull Duration reconciliationRepublishAfter,
        @NotNull Duration staleTaskTimeout,
        @PositiveOrZero int maxTaskRetries
) {
    public WorkerProperties {
        requirePositive(outboxPublishDelay, "outboxPublishDelay");
        requirePositive(reconciliationDelay, "reconciliationDelay");
        requirePositive(reconciliationRepublishAfter, "reconciliationRepublishAfter");
        requirePositive(staleTaskTimeout, "staleTaskTimeout");
    }

    private static void requirePositive(Duration value, String name) {
        if (value != null && (value.isZero() || value.isNegative())) {
            throw new IllegalArgumentException(name + " must be positive");
        }
    }
}
