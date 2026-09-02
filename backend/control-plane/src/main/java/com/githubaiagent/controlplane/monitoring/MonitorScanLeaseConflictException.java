package com.githubaiagent.controlplane.monitoring;

public class MonitorScanLeaseConflictException extends RuntimeException {

    public MonitorScanLeaseConflictException(String scannerKey) {
        super("monitor scan lease token is stale or does not match: " + scannerKey);
    }
}
