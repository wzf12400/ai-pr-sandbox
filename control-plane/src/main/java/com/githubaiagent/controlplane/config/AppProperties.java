package com.githubaiagent.controlplane.config;

import jakarta.validation.Valid;
import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.NotEmpty;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.validation.annotation.Validated;

import java.util.List;

@Validated
@ConfigurationProperties(prefix = "app")
public record AppProperties(
        @NotBlank String policyId,
        @NotEmpty List<@Valid RepositoryDefinition> repositoryCatalog
) {
    public AppProperties {
        repositoryCatalog = repositoryCatalog == null ? List.of() : List.copyOf(repositoryCatalog);
    }

    public record RepositoryDefinition(
            @NotBlank String repository,
            @NotEmpty List<@NotBlank String> keywords
    ) {
        public RepositoryDefinition {
            keywords = keywords == null ? List.of() : List.copyOf(keywords);
        }
    }
}
