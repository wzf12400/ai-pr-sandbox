package com.githubaiagent.controlplane.task;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

class NaturalLanguageSanitizerTest {

    private final NaturalLanguageSanitizer sanitizer = new NaturalLanguageSanitizer();

    @Test
    void redactsCommonSensitiveValuesBeforePersistence() {
        String sanitized = sanitizer.sanitize(
                "联系 person@example.com，password=hunter2，Authorization Bearer abc.def.ghi"
        );

        assertThat(sanitized)
                .doesNotContain("person@example.com", "hunter2", "abc.def.ghi")
                .contains("[REDACTED_EMAIL]", "password=[REDACTED]", "Bearer [REDACTED]");
    }

    @Test
    void normalizesWhitespace() {
        assertThat(sanitizer.sanitize("  支付\n\n订单   分页  "))
                .isEqualTo("支付 订单 分页");
    }
}
