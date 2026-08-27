package com.githubaiagent.controlplane.routing;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;
import java.util.Optional;

public interface LogRepositoryRouteRepository
        extends JpaRepository<LogRepositoryRoute, String> {

    List<LogRepositoryRoute> findAllByOrderByRepositoryAscSelectorValueAsc();

    List<LogRepositoryRoute> findAllByEnabledTrueOrderByRepositoryAscSelectorValueAsc();

    Optional<LogRepositoryRoute> findByIdAndEnabledTrue(String id);

    Optional<LogRepositoryRoute> findBySelectorFieldAndSelectorValue(
            String selectorField,
            String selectorValue
    );

    boolean existsBySelectorFieldAndSelectorValue(String selectorField, String selectorValue);

    boolean existsBySelectorFieldAndSelectorValueAndIdNot(
            String selectorField,
            String selectorValue,
            String id
    );
}
