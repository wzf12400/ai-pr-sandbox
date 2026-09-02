package com.githubaiagent.controlplane.routing;

import com.githubaiagent.controlplane.routing.api.JiraProjectBindingRequest;
import com.githubaiagent.controlplane.routing.api.JiraRepositoryRouteRequest;
import com.githubaiagent.controlplane.routing.api.JiraRepositoryRouteResponse;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.server.ResponseStatusException;

import java.time.Instant;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;

import static org.springframework.http.HttpStatus.NOT_FOUND;

@Service
public class JiraRepositoryRouteService {

    public static final String PROJECT_MATCH_TYPE = "PROJECT";
    public static final String PROJECT_MATCH_VALUE = "*";
    public static final Set<String> SUPPORTED_MATCH_TYPES = Set.of(
            PROJECT_MATCH_TYPE,
            "COMPONENT",
            "LABEL"
    );

    private static final Pattern PROJECT_KEY_PATTERN =
            Pattern.compile("[A-Z][A-Z0-9_]{0,19}");
    private static final Pattern REPOSITORY_PATTERN =
            Pattern.compile("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+");
    private static final Pattern CONTROL_CHARACTERS =
            Pattern.compile("[\\p{Cc}\\p{Cf}]");

    private final JiraRepositoryRouteRepository routeRepository;

    public JiraRepositoryRouteService(JiraRepositoryRouteRepository routeRepository) {
        this.routeRepository = routeRepository;
    }

    @Transactional(readOnly = true)
    public List<JiraRepositoryRouteResponse> list(boolean enabledOnly) {
        List<JiraRepositoryRoute> routes = enabledOnly
                ? routeRepository
                        .findAllByEnabledTrueOrderByProjectKeyAscPriorityAscRepositoryAsc()
                : routeRepository.findAllByOrderByProjectKeyAscPriorityAscRepositoryAsc();
        return routes.stream().map(JiraRepositoryRouteResponse::from).toList();
    }

    @Transactional
    public JiraRepositoryRouteResponse create(JiraRepositoryRouteRequest request) {
        ValidatedRoute values = validate(request);
        Instant now = Instant.now();
        JiraRepositoryRoute route = new JiraRepositoryRoute(
                UUID.randomUUID().toString(),
                values.projectKey(),
                values.projectName(),
                values.matchType(),
                values.matchValue(),
                values.repository(),
                values.enabled(),
                values.priority(),
                now
        );
        try {
            return JiraRepositoryRouteResponse.from(routeRepository.saveAndFlush(route));
        } catch (DataIntegrityViolationException exception) {
            throw duplicateRoute(exception);
        }
    }

    @Transactional
    public JiraRepositoryRouteResponse update(
            String routeId,
            JiraRepositoryRouteRequest request
    ) {
        ValidatedRoute values = validate(request);
        JiraRepositoryRoute route = find(routeId);
        if (routeRepository
                .existsByProjectKeyAndMatchTypeAndMatchValueAndRepositoryAndIdNot(
                        values.projectKey(),
                        values.matchType(),
                        values.matchValue(),
                        values.repository(),
                        routeId
                )) {
            throw duplicateRoute(null);
        }
        route.update(
                values.projectKey(),
                values.projectName(),
                values.matchType(),
                values.matchValue(),
                values.repository(),
                values.enabled(),
                values.priority(),
                Instant.now()
        );
        try {
            routeRepository.flush();
        } catch (DataIntegrityViolationException exception) {
            throw duplicateRoute(exception);
        }
        return JiraRepositoryRouteResponse.from(route);
    }

    @Transactional
    public JiraRepositoryRouteResponse upsertProjectBinding(
            JiraProjectBindingRequest request
    ) {
        ValidatedRoute values = validate(new JiraRepositoryRouteRequest(
                request.projectKey(),
                request.projectName(),
                PROJECT_MATCH_TYPE,
                PROJECT_MATCH_VALUE,
                request.repository(),
                true,
                100
        ));
        Instant now = Instant.now();
        List<JiraRepositoryRoute> repositoryProjectBindings =
                routeRepository.findAllByRepositoryAndMatchType(
                        values.repository(),
                        PROJECT_MATCH_TYPE
                );
        routeRepository.deleteAll(repositoryProjectBindings.stream()
                .filter(route -> !route.getProjectKey().equals(values.projectKey()))
                .toList());
        routeRepository.findAllByProjectKey(values.projectKey()).stream()
                .filter(route -> !route.getProjectName().equals(values.projectName()))
                .forEach(route -> route.renameProject(values.projectName(), now));
        JiraRepositoryRoute route = routeRepository
                .findByProjectKeyAndMatchTypeAndMatchValueAndRepository(
                        values.projectKey(),
                        PROJECT_MATCH_TYPE,
                        PROJECT_MATCH_VALUE,
                        values.repository()
                )
                .orElseGet(() -> routeRepository.save(new JiraRepositoryRoute(
                        UUID.randomUUID().toString(),
                        values.projectKey(),
                        values.projectName(),
                        PROJECT_MATCH_TYPE,
                        PROJECT_MATCH_VALUE,
                        values.repository(),
                        true,
                        100,
                        now
                )));
        route.update(
                values.projectKey(),
                values.projectName(),
                PROJECT_MATCH_TYPE,
                PROJECT_MATCH_VALUE,
                values.repository(),
                true,
                100,
                now
        );
        try {
            routeRepository.flush();
        } catch (DataIntegrityViolationException exception) {
            throw duplicateRoute(exception);
        }
        return JiraRepositoryRouteResponse.from(route);
    }

    @Transactional
    public void deleteProjectBinding(String repository) {
        String validatedRepository = validateRepository(repository);
        routeRepository.deleteAll(
                routeRepository.findAllByRepositoryAndMatchType(
                        validatedRepository,
                        PROJECT_MATCH_TYPE
                )
        );
    }

    @Transactional
    public void delete(String routeId) {
        routeRepository.delete(find(routeId));
    }

    private JiraRepositoryRoute find(String routeId) {
        return routeRepository.findById(routeId)
                .orElseThrow(() -> new ResponseStatusException(
                        NOT_FOUND, "Jira repository route not found"));
    }

    private ValidatedRoute validate(JiraRepositoryRouteRequest request) {
        String projectKey = request.projectKey().trim().toUpperCase();
        String projectName = request.projectName().trim();
        String matchType = request.matchType().trim().toUpperCase();
        String matchValue = request.matchValue().trim();
        String repository = request.repository().trim();
        if (!PROJECT_KEY_PATTERN.matcher(projectKey).matches()) {
            throw new IllegalArgumentException("Jira project key is invalid");
        }
        if (projectName.isEmpty() || CONTROL_CHARACTERS.matcher(projectName).find()) {
            throw new IllegalArgumentException("Jira project name is invalid");
        }
        if (!SUPPORTED_MATCH_TYPES.contains(matchType)) {
            throw new IllegalArgumentException("unsupported Jira match type");
        }
        if (PROJECT_MATCH_TYPE.equals(matchType)
                && !PROJECT_MATCH_VALUE.equals(matchValue)) {
            throw new IllegalArgumentException("project match value must be *");
        }
        if (!PROJECT_MATCH_TYPE.equals(matchType)
                && (matchValue.isEmpty()
                || CONTROL_CHARACTERS.matcher(matchValue).find())) {
            throw new IllegalArgumentException("Jira match value is invalid");
        }
        repository = validateRepository(repository);
        if (request.priority() < 0 || request.priority() > 10_000) {
            throw new IllegalArgumentException("Jira route priority is invalid");
        }
        return new ValidatedRoute(
                projectKey,
                projectName,
                matchType,
                matchValue,
                repository,
                request.enabled(),
                request.priority()
        );
    }

    private IllegalArgumentException duplicateRoute(Exception cause) {
        return new IllegalArgumentException(
                "Jira repository route is already mapped",
                cause
        );
    }

    private String validateRepository(String repository) {
        String normalized = repository.trim();
        if (!REPOSITORY_PATTERN.matcher(normalized).matches()) {
            throw new IllegalArgumentException("repository is invalid");
        }
        return normalized;
    }

    private record ValidatedRoute(
            String projectKey,
            String projectName,
            String matchType,
            String matchValue,
            String repository,
            boolean enabled,
            int priority
    ) {
    }
}
