package com.githubaiagent.controlplane.routing.api;

import com.githubaiagent.controlplane.routing.LogRepositoryRouteService;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/log-repository-routes")
public class LogRepositoryRouteController {

    private final LogRepositoryRouteService routeService;

    public LogRepositoryRouteController(LogRepositoryRouteService routeService) {
        this.routeService = routeService;
    }

    @GetMapping
    public List<LogRepositoryRouteResponse> list(
            @RequestParam(defaultValue = "false") boolean enabledOnly
    ) {
        return routeService.list(enabledOnly);
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public LogRepositoryRouteResponse create(
            @Valid @RequestBody LogRepositoryRouteRequest request
    ) {
        return routeService.create(request);
    }

    @PutMapping("/{routeId}")
    public LogRepositoryRouteResponse update(
            @PathVariable String routeId,
            @Valid @RequestBody LogRepositoryRouteRequest request
    ) {
        return routeService.update(routeId, request);
    }

    @PutMapping("/sync-repositories")
    public List<LogRepositoryRouteResponse> syncRepositories(
            @Valid @RequestBody SyncLogRepositoryRoutesRequest request
    ) {
        return routeService.syncRepositories(request.repositories());
    }

    @DeleteMapping("/{routeId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void delete(@PathVariable String routeId) {
        routeService.delete(routeId);
    }
}
