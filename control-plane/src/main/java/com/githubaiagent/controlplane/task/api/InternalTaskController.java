package com.githubaiagent.controlplane.task.api;

import com.githubaiagent.controlplane.task.ActorType;
import com.githubaiagent.controlplane.task.TaskService;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.bind.annotation.ResponseStatus;

@RestController
@RequestMapping("/api/internal/tasks")
public class InternalTaskController {

    private final TaskService taskService;

    public InternalTaskController(TaskService taskService) {
        this.taskService = taskService;
    }

    @PostMapping("/{taskId}/claim")
    public TaskClaimResponse claim(@PathVariable String taskId) {
        return taskService.claim(taskId);
    }

    @PostMapping("/{taskId}/transitions")
    public TaskResponse transition(
            @PathVariable String taskId,
            @Valid @RequestBody TransitionTaskRequest request
    ) {
        return taskService.transition(
                taskId,
                request.targetStatus(),
                request.detail(),
                ActorType.MOCK_WORKER
        );
    }

    @PostMapping("/{taskId}/issue")
    public TaskResponse attachIssue(
            @PathVariable String taskId,
            @Valid @RequestBody AttachIssueRequest request
    ) {
        return taskService.attachIssue(taskId, request.issueNumber(), request.issueUrl());
    }

    @PostMapping("/{taskId}/pull-request")
    public TaskResponse attachPullRequest(
            @PathVariable String taskId,
            @Valid @RequestBody AttachPullRequestRequest request
    ) {
        return taskService.attachPullRequest(
                taskId,
                request.prNumber(),
                request.prUrl(),
                request.testSummary()
        );
    }

    @PostMapping("/{taskId}/enqueue")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void enqueue(@PathVariable String taskId) {
        taskService.requeue(taskId);
    }
}
