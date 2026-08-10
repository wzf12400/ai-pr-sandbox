package com.githubaiagent.controlplane.task.api;

import com.githubaiagent.controlplane.task.AutomationJob;

import java.time.Instant;
import java.util.Arrays;
import java.util.List;

public record LogIncidentView(
        String sourceReference,
        Instant firstSeenAt,
        Instant lastSeenAt,
        Integer currentScanEventCount,
        Integer historicalEventCount,
        Integer incidentGroupCount,
        List<String> affectedEndpoints,
        Integer affectedUserCountMin,
        Integer affectedUserCountMax,
        Integer userIdentifierEventCount,
        Boolean historicalCountComplete,
        String aggregationBasis
) {
    public static LogIncidentView from(AutomationJob job) {
        if (job.getSourceReference() == null) {
            return null;
        }
        List<String> endpoints = job.getAffectedEndpoints() == null
                || job.getAffectedEndpoints().isBlank()
                ? List.of()
                : Arrays.asList(job.getAffectedEndpoints().split("\\n"));
        return new LogIncidentView(
                job.getSourceReference(),
                job.getFirstSeenAt(),
                job.getLastSeenAt(),
                job.getCurrentScanEventCount(),
                job.getHistoricalEventCount(),
                job.getIncidentGroupCount(),
                endpoints,
                job.getAffectedUserCountMin(),
                job.getAffectedUserCountMax(),
                job.getUserIdentifierEventCount(),
                job.getHistoricalCountComplete(),
                job.getAggregationBasis()
        );
    }
}
