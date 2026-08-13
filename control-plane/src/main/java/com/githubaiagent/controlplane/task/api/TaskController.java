package com.githubaiagent.controlplane.task.api;

import com.githubaiagent.controlplane.task.TaskService;
import jakarta.validation.Valid;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

import java.util.List;

@RestController
@RequestMapping("/api/tasks")
public class TaskController {

    private final TaskService taskService;

    public TaskController(TaskService taskService) {
        this.taskService = taskService;
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    public TaskResponse create(@Valid @RequestBody CreateTaskRequest request) {
        return taskService.create(request);
    }

    @GetMapping
    public List<TaskResponse> list() {
        return taskService.list();
    }

    @GetMapping("/{taskId}")
    public TaskDetailResponse detail(@PathVariable String taskId) {
        return taskService.detail(taskId);
    }

    @PostMapping("/{taskId}/messages")
    public TaskDetailResponse postMessage(
            @PathVariable String taskId,
            @Valid @RequestBody TaskMessageRequest request
    ) {
        return taskService.postMessage(taskId, request);
    }

    @DeleteMapping("/{taskId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    public void delete(@PathVariable String taskId) {
        taskService.delete(taskId);
    }
}
