package com.githubaiagent.controlplane.routing;

import com.githubaiagent.controlplane.routing.api.LogRepositoryRouteRequest;
import com.githubaiagent.controlplane.routing.api.LogRepositoryRouteResponse;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.dao.DataIntegrityViolationException;

import java.time.Instant;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Pattern;

import static org.springframework.http.HttpStatus.NOT_FOUND;

@Service
public class LogRepositoryRouteService {

    public static final Set<String> SUPPORTED_SELECTOR_FIELDS = Set.of(
            "kubernetes.container_name.keyword",
            "kubernetes.labels.app_kubernetes_io/name.keyword"
    );

    private static final Pattern SAFE_SELECTOR_VALUE = Pattern.compile(
            "[a-z0-9](?:[a-z0-9.-]{0,126}[a-z0-9])?"
    );

    private final LogRepositoryRouteRepository routeRepository;
    private final RepositoryMatcher repositoryMatcher;

    public LogRepositoryRouteService(
            LogRepositoryRouteRepository routeRepository,
            RepositoryMatcher repositoryMatcher
    ) {
        this.routeRepository = routeRepository;
        this.repositoryMatcher = repositoryMatcher;
    }

    @Transactional(readOnly = true)
    public List<LogRepositoryRouteResponse> list(boolean enabledOnly) {
        List<LogRepositoryRoute> routes = enabledOnly
                ? routeRepository.findAllByEnabledTrueOrderByRepositoryAscSelectorValueAsc()
                : routeRepository.findAllByOrderByRepositoryAscSelectorValueAsc();
        return routes.stream()
                .filter(route -> !enabledOnly
                        || repositoryMatcher.isAuthorized(route.getRepository()))
                .map(LogRepositoryRouteResponse::from)
                .toList();
    }

    @Transactional
    public LogRepositoryRouteResponse create(LogRepositoryRouteRequest request) {
        ValidatedRoute values = validate(request);
        if (routeRepository.existsBySelectorFieldAndSelectorValue(
                values.selectorField(), values.selectorValue())) {
            throw new IllegalArgumentException("log selector is already mapped");
        }
        Instant now = Instant.now();
        LogRepositoryRoute route = new LogRepositoryRoute(
                UUID.randomUUID().toString(),
                values.selectorField(),
                values.selectorValue(),
                values.repository(),
                values.enabled(),
                now
        );
        try {
            return LogRepositoryRouteResponse.from(routeRepository.saveAndFlush(route));
        } catch (DataIntegrityViolationException exception) {
            throw new IllegalArgumentException("log selector is already mapped", exception);
        }
    }

    @Transactional
    public LogRepositoryRouteResponse update(
            String routeId,
            LogRepositoryRouteRequest request
    ) {
        ValidatedRoute values = validate(request);
        LogRepositoryRoute route = find(routeId);
        if (routeRepository.existsBySelectorFieldAndSelectorValueAndIdNot(
                values.selectorField(), values.selectorValue(), routeId)) {
            throw new IllegalArgumentException("log selector is already mapped");
        }
        route.update(
                values.selectorField(),
                values.selectorValue(),
                values.repository(),
                values.enabled(),
                Instant.now()
        );
        try {
            routeRepository.flush();
        } catch (DataIntegrityViolationException exception) {
            throw new IllegalArgumentException("log selector is already mapped", exception);
        }
        return LogRepositoryRouteResponse.from(route);
    }

    @Transactional
    public void delete(String routeId) {
        routeRepository.delete(find(routeId));
    }

    @Transactional
    public List<LogRepositoryRouteResponse> syncRepositories(List<String> repositories) {
        Instant now = Instant.now();
        List<LogRepositoryRoute> routes = repositories.stream()
                .map(String::trim)
                .distinct()
                .map(repository -> upsertRepository(repository, now))
                .toList();
        try {
            routeRepository.flush();
        } catch (DataIntegrityViolationException exception) {
            throw new IllegalArgumentException(
                    "log selector is already mapped by another repository", exception);
        }
        return routes.stream().map(LogRepositoryRouteResponse::from).toList();
    }

    @Transactional(readOnly = true)
    public Optional<LogRepositoryRoute> resolveEnabled(String routeId) {
        return routeRepository.findByIdAndEnabledTrue(routeId)
                .filter(route -> repositoryMatcher.isAuthorized(route.getRepository()))
                .filter(route -> SUPPORTED_SELECTOR_FIELDS.contains(route.getSelectorField()))
                .filter(route -> SAFE_SELECTOR_VALUE.matcher(route.getSelectorValue()).matches());
    }

    private LogRepositoryRoute find(String routeId) {
        return routeRepository.findById(routeId)
                .orElseThrow(() -> new ResponseStatusException(
                        NOT_FOUND, "log repository route not found"));
    }

    private LogRepositoryRoute upsertRepository(String repository, Instant now) {
        if (!repository.matches("[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")) {
            throw new IllegalArgumentException("repository is invalid");
        }
        String selectorValue = repository.substring(repository.indexOf('/') + 1)
                .toLowerCase();
        if (!SAFE_SELECTOR_VALUE.matcher(selectorValue).matches()) {
            throw new IllegalArgumentException(
                    "repository name cannot be used as a log selector");
        }
        String selectorField = "kubernetes.container_name.keyword";
        Optional<LogRepositoryRoute> existing =
                routeRepository.findBySelectorFieldAndSelectorValue(
                        selectorField, selectorValue);
        if (existing.isPresent()) {
            LogRepositoryRoute route = existing.get();
            if (!route.getRepository().equals(repository)) {
                throw new IllegalArgumentException(
                        "log selector is already mapped to another repository");
            }
            route.update(selectorField, selectorValue, repository, true, now);
            return route;
        }
        LogRepositoryRoute route = new LogRepositoryRoute(
                UUID.randomUUID().toString(),
                selectorField,
                selectorValue,
                repository,
                true,
                now
        );
        return routeRepository.save(route);
    }

    private ValidatedRoute validate(LogRepositoryRouteRequest request) {
        String selectorField = request.selectorField().trim();
        String selectorValue = request.selectorValue().trim();
        String repository = request.repository().trim();
        if (!SUPPORTED_SELECTOR_FIELDS.contains(selectorField)) {
            throw new IllegalArgumentException("unsupported log selector field");
        }
        if (!SAFE_SELECTOR_VALUE.matcher(selectorValue).matches()) {
            throw new IllegalArgumentException("log selector value is invalid");
        }
        if (!repositoryMatcher.isAuthorized(repository)) {
            throw new IllegalArgumentException(
                    "log route repository is not in the authorized catalog");
        }
        return new ValidatedRoute(
                selectorField, selectorValue, repository, request.enabled());
    }

    private record ValidatedRoute(
            String selectorField,
            String selectorValue,
            String repository,
            boolean enabled
    ) {
    }
}
