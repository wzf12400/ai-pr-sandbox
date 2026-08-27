package com.githubaiagent.controlplane.monitoring.api;

import com.githubaiagent.controlplane.monitoring.MonitorScanCoordinationService;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/internal/scan-coordination")
public class MonitorScanCoordinationController {

    private final MonitorScanCoordinationService coordinationService;

    public MonitorScanCoordinationController(
            MonitorScanCoordinationService coordinationService
    ) {
        this.coordinationService = coordinationService;
    }

    @GetMapping("/{scannerKey}")
    public MonitorScanCoordinationResponse get(@PathVariable String scannerKey) {
        return coordinationService.get(scannerKey);
    }

    @PostMapping("/{scannerKey}/acquire")
    public MonitorScanCoordinationResponse acquire(
            @PathVariable String scannerKey,
            @Valid @RequestBody AcquireMonitorScanLeaseRequest request
    ) {
        return coordinationService.acquire(scannerKey, request);
    }

    @PostMapping("/{scannerKey}/complete")
    public MonitorScanCoordinationResponse complete(
            @PathVariable String scannerKey,
            @Valid @RequestBody CompleteMonitorScanRequest request
    ) {
        return coordinationService.complete(scannerKey, request);
    }
}
