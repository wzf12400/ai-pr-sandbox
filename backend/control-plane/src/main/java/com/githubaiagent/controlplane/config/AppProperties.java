package com.githubaiagent.controlplane.config;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.validation.annotation.Validated;

import java.util.List;
import java.util.Set;
import java.util.stream.Collectors;

@Validated
@ConfigurationProperties(prefix = "app")
public record AppProperties(
        @NotBlank String policyId,
        @NotEmpty List<@Valid RepositoryDefinition> repositoryCatalog
) {
    public AppProperties {
        repositoryCatalog = repositoryCatalog == null ? List.of() : List.copyOf(repositoryCatalog);
        Set<String> repositories = repositoryCatalog.stream()
                .map(RepositoryDefinition::repository)
                .collect(Collectors.toUnmodifiableSet());
        for (RepositoryDefinition definition : repositoryCatalog) {
            if (definition.dependencies().contains(definition.repository())
                    || !repositories.containsAll(definition.dependencies())) {
                throw new IllegalArgumentException(
                        "repository dependencies must reference other catalog repositories"
                );
            }
        }
    }

    public record RepositoryDefinition(
            @NotBlank String repository,
            @NotEmpty List<@NotBlank String> keywords,
            List<@NotBlank String> dependencies
    ) {
        public RepositoryDefinition {
            keywords = keywords == null ? List.of() : List.copyOf(keywords);
            dependencies = dependencies == null ? List.of() : List.copyOf(dependencies);
        }
    }
}
