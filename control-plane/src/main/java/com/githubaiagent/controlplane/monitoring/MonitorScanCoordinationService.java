package com.githubaiagent.controlplane.monitoring;

import com.githubaiagent.controlplane.monitoring.api.AcquireMonitorScanLeaseRequest;
import com.githubaiagent.controlplane.monitoring.api.CompleteMonitorScanRequest;
import com.githubaiagent.controlplane.monitoring.api.MonitorScanCoordinationResponse;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.core.type.TypeReference;
import tools.jackson.databind.ObjectMapper;

import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;

@Service
public class MonitorScanCoordinationService {

    private static final Set<String> SUPPORTED_SCANNER_KEYS = Set.of("JIRA", "LOG");
    private static final int MAX_CHECKPOINT_BYTES = 1_000_000;
    private static final int MAX_ERROR_CODE_POINTS = 500;
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {
    };

    private final MonitorScanCoordinationRepository coordinationRepository;
    private final ObjectMapper objectMapper;

    public MonitorScanCoordinationService(
            MonitorScanCoordinationRepository coordinationRepository,
            ObjectMapper objectMapper
    ) {
        this.coordinationRepository = coordinationRepository;
        this.objectMapper = objectMapper;
    }

    @Transactional(readOnly = true)
    public MonitorScanCoordinationResponse get(String scannerKey) {
        MonitorScanCoordination coordination = coordinationRepository.findById(
                normalizeScannerKey(scannerKey)
        ).orElseThrow(() -> new MonitorScanCoordinationNotFoundException(scannerKey));
        return response(coordination, false, null);
    }

    @Transactional
    public MonitorScanCoordinationResponse acquire(
            String scannerKey,
            AcquireMonitorScanLeaseRequest request
    ) {
        String normalizedScannerKey = normalizeScannerKey(scannerKey);
        MonitorScanCoordination coordination = findForUpdate(normalizedScannerKey);
        Instant now = Instant.now();
        if (coordination.hasActiveLease(now)) {
            return response(coordination, false, null);
        }

        String token = UUID.randomUUID().toString();
        coordination.acquire(
                request.ownerId(),
                token,
                now.plusSeconds(request.leaseSeconds()),
                now
        );
        return response(coordination, true, token);
    }

    @Transactional
    public MonitorScanCoordinationResponse complete(
            String scannerKey,
            CompleteMonitorScanRequest request
    ) {
        String normalizedScannerKey = normalizeScannerKey(scannerKey);
        MonitorScanCoordination coordination = findForUpdate(normalizedScannerKey);
        if (!request.leaseToken().equals(coordination.getLeaseToken())) {
            throw new MonitorScanLeaseConflictException(normalizedScannerKey);
        }

        coordination.complete(
                serializeCheckpoint(request.checkpoint()),
                boundedError(request.error()),
                Instant.now()
        );
        return response(coordination, false, null);
    }

    private MonitorScanCoordination findForUpdate(String scannerKey) {
        return coordinationRepository.findByScannerKeyForUpdate(scannerKey)
                .orElseThrow(() -> new MonitorScanCoordinationNotFoundException(scannerKey));
    }

    private String normalizeScannerKey(String scannerKey) {
        String normalized = scannerKey == null ? "" : scannerKey.trim().toUpperCase(Locale.ROOT);
        if (!SUPPORTED_SCANNER_KEYS.contains(normalized)) {
            throw new MonitorScanCoordinationNotFoundException(scannerKey);
        }
        return normalized;
    }

    private String serializeCheckpoint(Map<String, Object> checkpoint) {
        try {
            String serialized = objectMapper.writeValueAsString(checkpoint);
            if (serialized.getBytes(StandardCharsets.UTF_8).length > MAX_CHECKPOINT_BYTES) {
                throw new IllegalArgumentException(
                        "checkpoint must not exceed 1000000 UTF-8 bytes"
                );
            }
            return serialized;
        } catch (IllegalArgumentException exception) {
            throw exception;
        } catch (Exception exception) {
            throw new IllegalArgumentException("checkpoint cannot be serialized", exception);
        }
    }

    private String boundedError(String error) {
        if (error == null) {
            return null;
        }
        int codePointCount = error.codePointCount(0, error.length());
        if (codePointCount <= MAX_ERROR_CODE_POINTS) {
            return error;
        }
        return error.substring(0, error.offsetByCodePoints(0, MAX_ERROR_CODE_POINTS));
    }

    private MonitorScanCoordinationResponse response(
            MonitorScanCoordination coordination,
            boolean acquired,
            String leaseToken
    ) {
        return new MonitorScanCoordinationResponse(
                coordination.getScannerKey(),
                acquired,
                leaseToken,
                coordination.getLeaseExpiresAt(),
                checkpointFrom(coordination.getCheckpointJson()),
                coordination.getLastStartedAt(),
                coordination.getLastCompletedAt(),
                coordination.getLastError()
        );
    }

    private Map<String, Object> checkpointFrom(String checkpointJson) {
        if (checkpointJson == null || checkpointJson.isBlank()) {
            return Map.of();
        }
        try {
            return objectMapper.readValue(checkpointJson, MAP_TYPE);
        } catch (Exception exception) {
            throw new IllegalStateException("stored checkpoint is not a JSON object", exception);
        }
    }
}
