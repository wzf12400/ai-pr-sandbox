package com.githubaiagent.controlplane.routing.api;

import com.githubaiagent.controlplane.routing.JiraRepositoryRouteService;
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
import java.util.Map;

@RestController
@RequestMapping("/api/jira-repository-routes")
public class JiraRepositoryRouteController {

    private final JiraRepositoryRouteService routeService;

    public JiraRepositoryRouteController(JiraRepositoryRouteService routeService) {
        this.routeService = routeService;
    }

    @GetMapping
    public List<JiraRepositoryRouteResponse> list(
            @RequestParam(defaultValue = "false") boolean enabledOnly
    ) {
        return routeService.list(enabledOnly);
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public JiraRepositoryRouteResponse create(
            @Valid @RequestBody JiraRepositoryRouteRequest request
    ) {
        return routeService.create(request);
    }

    @PutMapping("/{routeId}")
    public JiraRepositoryRouteResponse update(
            @PathVariable String routeId,
            @Valid @RequestBody JiraRepositoryRouteRequest request
    ) {
        return routeService.update(routeId, request);
    }

    @PutMapping("/project-binding")
    public JiraRepositoryRouteResponse upsertProjectBinding(
            @Valid @RequestBody JiraProjectBindingRequest request
    ) {
        return routeService.upsertProjectBinding(request);
    }

    @DeleteMapping("/project-binding")
    public Map<String, String> deleteProjectBinding(
            @RequestParam String repository
    ) {
        routeService.deleteProjectBinding(repository);
        return Map.of("status", "deleted");
    }

    @DeleteMapping("/{routeId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void delete(@PathVariable String routeId) {
        routeService.delete(routeId);
    }
}
