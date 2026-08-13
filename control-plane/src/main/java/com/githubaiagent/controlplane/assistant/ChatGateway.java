package com.githubaiagent.controlplane.assistant;

import java.util.Map;

public interface ChatGateway {

    boolean isActive();

    AssistantAnswer converse(String systemPrompt, Map<String, Object> userPayload);
}
