package com.githubaiagent.controlplane.routing;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface JiraRepositoryRouteRepository
        extends JpaRepository<JiraRepositoryRoute, String> {

    List<JiraRepositoryRoute> findAllByOrderByProjectKeyAscPriorityAscRepositoryAsc();

    List<JiraRepositoryRoute>
            findAllByEnabledTrueOrderByProjectKeyAscPriorityAscRepositoryAsc();

    List<JiraRepositoryRoute> findAllByProjectKey(String projectKey);

    List<JiraRepositoryRoute> findAllByRepositoryAndMatchType(
            String repository,
            String matchType
    );

    Optional<JiraRepositoryRoute>
            findByProjectKeyAndMatchTypeAndMatchValueAndRepository(
                    String projectKey,
                    String matchType,
                    String matchValue,
                    String repository
            );

    boolean existsByProjectKeyAndMatchTypeAndMatchValueAndRepositoryAndIdNot(
            String projectKey,
            String matchType,
            String matchValue,
            String repository,
            String id
    );
}
