package com.githubaiagent.controlplane.task.api;

import com.githubaiagent.controlplane.monitoring.MonitorScanCoordinationNotFoundException;
import com.githubaiagent.controlplane.monitoring.MonitorScanLeaseConflictException;
import com.githubaiagent.controlplane.configuration.ConfigurationProfileNotFoundException;
import com.githubaiagent.controlplane.configuration.ConfigurationProfileVersionConflictException;
import com.githubaiagent.controlplane.task.InvalidTaskTransitionException;
import com.githubaiagent.controlplane.task.TaskNotFoundException;
import com.githubaiagent.controlplane.worker.TaskClaimConflictException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ProblemDetail;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
public class ApiExceptionHandler {

    @ExceptionHandler({
            TaskNotFoundException.class,
            MonitorScanCoordinationNotFoundException.class,
            ConfigurationProfileNotFoundException.class
    })
    public ProblemDetail notFound(RuntimeException exception) {
        return problem(HttpStatus.NOT_FOUND, exception.getMessage());
    }

    @ExceptionHandler({InvalidTaskTransitionException.class, IllegalArgumentException.class})
    public ProblemDetail badRequest(RuntimeException exception) {
        return problem(HttpStatus.BAD_REQUEST, exception.getMessage());
    }

    @ExceptionHandler({
            TaskClaimConflictException.class,
            MonitorScanLeaseConflictException.class,
            ConfigurationProfileVersionConflictException.class
    })
    public ProblemDetail conflict(RuntimeException exception) {
        return problem(HttpStatus.CONFLICT, exception.getMessage());
    }

    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ProblemDetail validation(MethodArgumentNotValidException exception) {
        String detail = exception.getBindingResult().getFieldErrors().stream()
                .findFirst()
                .map(error -> error.getField() + " " + error.getDefaultMessage())
                .orElse("request validation failed");
        return problem(HttpStatus.BAD_REQUEST, detail);
    }

    private static ProblemDetail problem(HttpStatus status, String detail) {
        ProblemDetail problem = ProblemDetail.forStatusAndDetail(status, detail);
        problem.setTitle(status.getReasonPhrase());
        return problem;
    }
}
