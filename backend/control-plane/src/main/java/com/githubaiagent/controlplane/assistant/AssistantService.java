package com.githubaiagent.controlplane.assistant;

import com.githubaiagent.controlplane.config.AppProperties;
import com.githubaiagent.controlplane.task.AutomationJob;
import com.githubaiagent.controlplane.task.JobEvent;
import com.githubaiagent.controlplane.task.NaturalLanguageSanitizer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

@Service
public class AssistantService {

    private static final Logger log = LoggerFactory.getLogger(AssistantService.class);

    private static final String SYSTEM_PROMPT = """
            你是企业内部的代码变更助手，在严格的确定性门禁内工作。边界：
            1. 你只能帮助用户澄清需求、回答任务状态相关问题、从对话中提取仓库路由线索（服务名/模块名/文件路径/关键词）。
            2. 你不能执行、也不能声称执行任何写入操作（Issue、PR、代码修改）；仓库路由由确定性授权目录匹配最终决定，
               你提供的 routing_hints 仅作为匹配器的文本输入。
            3. 不要编造仓库、Issue 或 PR；不要输出密钥、令牌或个人信息。
            4. 回复使用简洁中文。信息不足时，在 missing_information 里明确指出还缺什么。
            输出严格 JSON：reply（给用户的回复）、routing_hints（从对话提取的路由线索）、missing_information（仍缺失的信息）。
            """;

    private static final int MAX_HISTORY_MESSAGES = 10;
    private static final int MAX_REPLY_CHARS = 900;
    private static final int MAX_HINT_CHARS = 120;

    private final ChatGateway chatGateway;
    private final NaturalLanguageSanitizer sanitizer;
    private final AppProperties properties;

    public AssistantService(
            ChatGateway chatGateway,
            NaturalLanguageSanitizer sanitizer,
            AppProperties properties
    ) {
        this.chatGateway = chatGateway;
        this.sanitizer = sanitizer;
        this.properties = properties;
    }

    public boolean isActive() {
        return chatGateway.isActive();
    }

    public Optional<AssistantAnswer> converse(
            AutomationJob job,
            List<JobEvent> priorEvents,
            String sanitizedUserMessage
    ) {
        return converse(
                TaskConversationContext.from(job),
                priorEvents,
                sanitizedUserMessage
        );
    }

    public Optional<AssistantAnswer> converse(
            TaskConversationContext task,
            List<JobEvent> priorEvents,
            String sanitizedUserMessage
    ) {
        if (!chatGateway.isActive()) {
            return Optional.empty();
        }
        try {
            AssistantAnswer answer = chatGateway.converse(
                    SYSTEM_PROMPT,
                    buildPayload(task, priorEvents, sanitizedUserMessage)
            );
            return Optional.of(sanitize(answer));
        } catch (RuntimeException e) {
            log.warn("chat gateway failed, falling back to deterministic reply: {}", e.toString());
            return Optional.empty();
        }
    }

    private Map<String, Object> buildPayload(
            TaskConversationContext taskContext,
            List<JobEvent> priorEvents,
            String sanitizedUserMessage
    ) {
        Map<String, Object> task = new LinkedHashMap<>();
        task.put("source_type", taskContext.sourceType());
        task.put("status", taskContext.status());
        task.put("requirement", taskContext.requirement());
        task.put("blocked_reason", taskContext.blockedReason());
        task.put("matched_repository", taskContext.matchedRepository());
        task.put("issue_url", taskContext.issueUrl());
        task.put("pr_url", taskContext.prUrl());

        List<Map<String, String>> catalog = new ArrayList<>();
        for (AppProperties.RepositoryDefinition definition : properties.repositoryCatalog()) {
            catalog.add(Map.of(
                    "repository", definition.repository(),
                    "keywords", String.join(", ", definition.keywords())
            ));
        }

        List<Map<String, String>> conversation = new ArrayList<>();
        List<JobEvent> messageEvents = priorEvents.stream()
                .filter(e -> "USER_MESSAGE".equals(e.getEventType())
                        || "AGENT_REPLY".equals(e.getEventType()))
                .toList();
        int from = Math.max(0, messageEvents.size() - MAX_HISTORY_MESSAGES);
        for (JobEvent event : messageEvents.subList(from, messageEvents.size())) {
            conversation.add(Map.of(
                    "role", "USER_MESSAGE".equals(event.getEventType()) ? "user" : "assistant",
                    "content", event.getDetail() == null ? "" : event.getDetail()
            ));
        }

        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("task", task);
        payload.put("authorized_catalog", catalog);
        payload.put("conversation", conversation);
        payload.put("new_user_message", sanitizedUserMessage);
        return payload;
    }

    private AssistantAnswer sanitize(AssistantAnswer answer) {
        String reply = sanitizer.sanitize(answer.reply());
        if (reply.length() > MAX_REPLY_CHARS) {
            reply = reply.substring(0, MAX_REPLY_CHARS);
        }
        List<String> hints = answer.routingHints().stream()
                .map(sanitizer::sanitize)
                .filter(h -> !h.isBlank())
                .map(h -> h.length() > MAX_HINT_CHARS ? h.substring(0, MAX_HINT_CHARS) : h)
                .limit(8)
                .toList();
        List<String> missing = answer.missingInformation().stream()
                .map(sanitizer::sanitize)
                .filter(m -> !m.isBlank())
                .limit(8)
                .toList();
        if (reply.isBlank()) {
            throw new ChatGatewayException("assistant reply was blank after sanitization");
        }
        return new AssistantAnswer(reply, hints, missing);
    }
}
