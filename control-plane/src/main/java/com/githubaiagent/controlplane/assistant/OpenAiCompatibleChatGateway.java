package com.githubaiagent.controlplane.assistant;

import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@Component
public class OpenAiCompatibleChatGateway implements ChatGateway {

    private static final Map<String, Object> ANSWER_SCHEMA = answerSchema();

    private final ChatProperties properties;
    private final ObjectMapper objectMapper;
    private final HttpClient httpClient;

    public OpenAiCompatibleChatGateway(ChatProperties properties, ObjectMapper objectMapper) {
        this.properties = properties;
        this.objectMapper = objectMapper;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(properties.timeoutSeconds()))
                .build();
    }

    @Override
    public boolean isActive() {
        return properties.active();
    }

    @Override
    public AssistantAnswer converse(String systemPrompt, Map<String, Object> userPayload) {
        if (!isActive()) {
            throw new ChatGatewayException("chat gateway is not configured");
        }
        try {
            return doConverse(systemPrompt, userPayload);
        } catch (ChatGatewayException e) {
            throw e;
        } catch (IOException e) {
            throw new ChatGatewayException("AI gateway request failed", e);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new ChatGatewayException("AI gateway request interrupted", e);
        }
    }

    private AssistantAnswer doConverse(String systemPrompt, Map<String, Object> userPayload)
            throws IOException, InterruptedException {
        Map<String, Object> requestPayload = new LinkedHashMap<>();
        requestPayload.put("model", properties.model());
        requestPayload.put("messages", List.of(
                Map.of("role", "system", "content", systemPrompt),
                Map.of(
                        "role",
                        "user",
                        "content",
                        objectMapper.writeValueAsString(userPayload)
                )
        ));
        requestPayload.put("stream", false);
        if ("compatible".equals(properties.apiMode())) {
            requestPayload.put("max_tokens", properties.maxCompletionTokens());
            requestPayload.put("response_format", Map.of("type", "json_object"));
        } else {
            requestPayload.put("max_completion_tokens", properties.maxCompletionTokens());
            requestPayload.put("response_format", Map.of(
                    "type",
                    "json_schema",
                    "json_schema",
                    Map.of("name", "task_conversation_reply", "strict", true, "schema", ANSWER_SCHEMA)
            ));
        }
        if (!properties.safetyIdentifier().isEmpty()) {
            requestPayload.put("safety_identifier", properties.safetyIdentifier());
        }

        HttpRequest request = HttpRequest.newBuilder()
                .uri(URI.create(properties.baseUrl() + "/chat/completions"))
                .timeout(Duration.ofSeconds(properties.timeoutSeconds()))
                .header("Authorization", "Bearer " + properties.apiKey())
                .header("Content-Type", "application/json")
                .header("Accept", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(
                        objectMapper.writeValueAsString(requestPayload)
                ))
                .build();

        HttpResponse<String> response = httpClient.send(
                request,
                HttpResponse.BodyHandlers.ofString()
        );
        if (response.statusCode() != 200) {
            throw new ChatGatewayException("AI gateway returned HTTP " + response.statusCode());
        }
        return parseAnswer(response.body());
    }

    private AssistantAnswer parseAnswer(String body) {
        try {
            JsonNode root = objectMapper.readTree(body);
            JsonNode contentNode = root.path("choices").path(0).path("message").path("content");
            if (!contentNode.isTextual()) {
                throw new ChatGatewayException("AI gateway returned an invalid structured response");
            }
            JsonNode content = objectMapper.readTree(contentNode.asText());
            String reply = content.path("reply").asText("").strip();
            if (reply.isEmpty()) {
                throw new ChatGatewayException("AI gateway structured response has an empty reply");
            }
            return new AssistantAnswer(
                    reply,
                    textList(content.path("routing_hints")),
                    textList(content.path("missing_information"))
            );
        } catch (ChatGatewayException e) {
            throw e;
        } catch (Exception e) {
            throw new ChatGatewayException("AI gateway returned an invalid structured response", e);
        }
    }

    private static List<String> textList(JsonNode node) {
        List<String> values = new ArrayList<>();
        if (node.isArray()) {
            for (JsonNode item : node) {
                String text = item.asText("").strip();
                if (!text.isEmpty() && values.size() < 8) {
                    values.add(text);
                }
            }
        }
        return values;
    }

    private static Map<String, Object> answerSchema() {
        Map<String, Object> stringArray = Map.of(
                "type", "array",
                "items", Map.of("type", "string")
        );
        Map<String, Object> properties = new LinkedHashMap<>();
        properties.put("reply", Map.of("type", "string"));
        properties.put("routing_hints", stringArray);
        properties.put("missing_information", stringArray);
        Map<String, Object> schema = new LinkedHashMap<>();
        schema.put("type", "object");
        schema.put("properties", properties);
        schema.put(
                "required",
                List.of("reply", "routing_hints", "missing_information")
        );
        schema.put("additionalProperties", false);
        return Map.copyOf(schema);
    }
}
