package com.githubaiagent.controlplane.routing;

import com.githubaiagent.controlplane.config.AppProperties;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

class CatalogRepositoryMatcherTest {

    private final CatalogRepositoryMatcher matcher = new CatalogRepositoryMatcher(
            new AppProperties(
                    "test-policy",
                    List.of(
                            new AppProperties.RepositoryDefinition(
                                    "demo-company/payment-service",
                                    List.of("支付", "订单", "payment", "order")
                            ),
                            new AppProperties.RepositoryDefinition(
                                    "demo-company/customer-portal",
                                    List.of("用户", "登录", "user", "login")
                            ),
                            new AppProperties.RepositoryDefinition(
                                    "wzf12400/ai-pr-sandbox",
                                    List.of("测试仓库", "计算器", "除法", "calculator", "divide")
                            )
                    )
            )
    );

    @Test
    void resolvesOneRepositoryWhenTwoIndependentKeywordsMatch() {
        RepositoryMatch match = matcher.match("支付订单列表最多显示 50 条");

        assertThat(match.status()).isEqualTo(RepositoryMatch.Status.RESOLVED);
        assertThat(match.repository()).isEqualTo("demo-company/payment-service");
        assertThat(match.confidence()).isEqualTo(50);
        assertThat(match.basis()).contains("支付", "订单");
    }

    @Test
    void needsContextWhenDifferentRepositoriesTie() {
        RepositoryMatch match = matcher.match("支付页面需要增加用户头像");

        assertThat(match.status()).isEqualTo(RepositoryMatch.Status.NEEDS_CONTEXT);
        assertThat(match.repository()).isNull();
        assertThat(match.candidates()).containsExactly(
                "demo-company/customer-portal",
                "demo-company/payment-service"
        );
    }

    @Test
    void needsContextWhenNoAuthorizedRepositoryMatches() {
        RepositoryMatch match = matcher.match("调整一个无法识别的内部组件");

        assertThat(match.status()).isEqualTo(RepositoryMatch.Status.NEEDS_CONTEXT);
        assertThat(match.candidates()).isEmpty();
        assertThat(match.confidence()).isZero();
    }

    @Test
    void resolvesTheAuthorizedGitHubSandboxRepository() {
        RepositoryMatch match = matcher.match("计算器除法遇到零时增加明确错误和测试");

        assertThat(match.status()).isEqualTo(RepositoryMatch.Status.RESOLVED);
        assertThat(match.repository()).isEqualTo("wzf12400/ai-pr-sandbox");
        assertThat(match.basis()).contains("计算器", "除法");
    }
}
