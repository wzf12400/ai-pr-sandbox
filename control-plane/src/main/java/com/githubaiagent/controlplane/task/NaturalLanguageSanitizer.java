package com.githubaiagent.controlplane.task;

import org.springframework.stereotype.Component;

import java.util.regex.Pattern;

@Component
public class NaturalLanguageSanitizer {

    private static final Pattern EMAIL = Pattern.compile(
            "(?i)\\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}\\b"
    );
    private static final Pattern BEARER_TOKEN = Pattern.compile(
            "(?i)\\bBearer\\s+[^\\s,;]+"
    );
    private static final Pattern NAMED_SECRET = Pattern.compile(
            "(?i)\\b(api[_-]?key|access[_-]?token|password|secret)\\s*[:=]\\s*[^\\s,;]+"
    );
    private static final Pattern WHITESPACE = Pattern.compile("\\s+");

    public String sanitize(String input) {
        String sanitized = EMAIL.matcher(input).replaceAll("[REDACTED_EMAIL]");
        sanitized = BEARER_TOKEN.matcher(sanitized).replaceAll("Bearer [REDACTED]");
        sanitized = NAMED_SECRET.matcher(sanitized).replaceAll("$1=[REDACTED]");
        return WHITESPACE.matcher(sanitized).replaceAll(" ").trim();
    }
}
