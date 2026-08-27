package com.githubaiagent.controlplane.routing;

public interface RepositoryMatcher {
    RepositoryMatch match(String sanitizedRequirement);

    /** 该仓库是否在授权目录内（用于校验外部传入的路由证据提示）。 */
    default boolean isAuthorized(String repository) {
        return false;
    }

    /** Whether one authorized repository may request an independent task in another repository. */
    default boolean isAuthorizedDependency(String sourceRepository, String targetRepository) {
        return false;
    }
}
