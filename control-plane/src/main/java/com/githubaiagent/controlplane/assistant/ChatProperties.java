package com.githubaiagent.controlplane.assistant;

import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.validation.annotation.Validated;

import java.util.Locale;

@Validated
@ConfigurationProperties(prefix = "app.chat")
public record ChatProperties(
        Boolean enabled,
        String baseUrl,
        String apiKey,
        String model,
        String safetyIdentifier,
        Integer timeoutSeconds,
        Integer maxCompletionTokens,
        String apiMode,
        Boolean asyncReply
) {
    public ChatProperties {
        enabled = enabled == null || enabled;
        asyncReply = asyncReply == null || asyncReply;
        baseUrl = baseUrl == null ? "" : baseUrl.strip();
        apiKey = apiKey == null ? "" : apiKey.strip();
        model = model == null || model.isBlank() ? "ailemac/gpt-5-mini" : model.strip();
        safetyIdentifier = safetyIdentifier == null ? "" : safetyIdentifier.strip();
        timeoutSeconds = timeoutSeconds == null ? 30 : timeoutSeconds;
        maxCompletionTokens = maxCompletionTokens == null ? 800 : maxCompletionTokens;
        apiMode = apiMode == null || apiMode.isBlank()
                ? "strict"
                : apiMode.strip().toLowerCase(Locale.ROOT);
        if (!apiMode.equals("strict") && !apiMode.equals("compatible")) {
            throw new IllegalArgumentException("app.chat.api-mode must be strict or compatible");
        }
        if (timeoutSeconds < 1 || timeoutSeconds > 300) {
            throw new IllegalArgumentException("app.chat.timeout-seconds must be between 1 and 300");
        }
        if (maxCompletionTokens < 128 || maxCompletionTokens > 10_000) {
            throw new IllegalArgumentException(
                    "app.chat.max-completion-tokens must be between 128 and 10000"
            );
        }
        if (!baseUrl.isEmpty()
                && !baseUrl.startsWith("https://")
                && !baseUrl.startsWith("http://127.0.0.1")
                && !baseUrl.startsWith("http://localhost")) {
            throw new IllegalArgumentException(
                    "app.chat.base-url must use HTTPS (plain HTTP is allowed only for local testing)"
            );
        }
        baseUrl = baseUrl.replaceAll("/+$", "");
    }

    public boolean active() {
        return enabled && !baseUrl.isEmpty() && !apiKey.isEmpty();
    }
}
