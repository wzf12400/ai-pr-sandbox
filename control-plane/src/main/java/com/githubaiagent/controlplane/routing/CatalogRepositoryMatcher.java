package com.githubaiagent.controlplane.routing;

import com.githubaiagent.controlplane.config.AppProperties;
import org.springframework.stereotype.Component;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;

@Component
public class CatalogRepositoryMatcher implements RepositoryMatcher {

    private static final int SCORE_PER_KEYWORD = 25;
    private static final int MINIMUM_RESOLVED_SCORE = 50;
    private static final int MINIMUM_MARGIN = 25;
    private static final int MAX_CANDIDATES = 3;

    private final AppProperties properties;

    public CatalogRepositoryMatcher(AppProperties properties) {
        this.properties = properties;
    }

    @Override
    public RepositoryMatch match(String sanitizedRequirement) {
        String normalized = sanitizedRequirement.toLowerCase(Locale.ROOT);
        List<ScoredRepository> scores = new ArrayList<>();
        for (AppProperties.RepositoryDefinition definition : properties.repositoryCatalog()) {
            List<String> matches = definition.keywords().stream()
                    .map(keyword -> keyword.toLowerCase(Locale.ROOT))
                    .filter(normalized::contains)
                    .distinct()
                    .sorted()
                    .toList();
            if (!matches.isEmpty()) {
                scores.add(new ScoredRepository(
                        definition.repository(),
                        Math.min(100, matches.size() * SCORE_PER_KEYWORD),
                        matches
                ));
            }
        }
        scores.sort(Comparator.comparingInt(ScoredRepository::score).reversed()
                .thenComparing(ScoredRepository::repository));

        List<String> candidates = scores.stream()
                .limit(MAX_CANDIDATES)
                .map(ScoredRepository::repository)
                .toList();
        if (scores.isEmpty()) {
            return new RepositoryMatch(
                    RepositoryMatch.Status.NEEDS_CONTEXT,
                    null,
                    "no authorized repository matched the sanitized requirement",
                    0,
                    List.of()
            );
        }

        ScoredRepository first = scores.getFirst();
        int secondScore = scores.size() > 1 ? scores.get(1).score() : 0;
        int margin = first.score() - secondScore;
        if (first.score() >= MINIMUM_RESOLVED_SCORE && margin >= MINIMUM_MARGIN) {
            return new RepositoryMatch(
                    RepositoryMatch.Status.RESOLVED,
                    first.repository(),
                    "matched authorized catalog keywords: " + String.join(", ", first.matches()),
                    first.score(),
                    candidates
            );
        }
        return new RepositoryMatch(
                RepositoryMatch.Status.NEEDS_CONTEXT,
                null,
                "repository match was ambiguous or lacked two independent keywords",
                first.score(),
                candidates
        );
    }

    @Override
    public boolean isAuthorized(String repository) {
        return properties.repositoryCatalog().stream()
                .anyMatch(definition -> definition.repository().equals(repository));
    }

    private record ScoredRepository(String repository, int score, List<String> matches) {
    }
}
