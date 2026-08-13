package com.githubaiagent.controlplane.assistant;

import java.util.List;

public record AssistantAnswer(
        String reply,
        List<String> routingHints,
        List<String> missingInformation
) {
    public AssistantAnswer {
        routingHints = routingHints == null ? List.of() : List.copyOf(routingHints);
        missingInformation =
                missingInformation == null ? List.of() : List.copyOf(missingInformation);
    }
}
