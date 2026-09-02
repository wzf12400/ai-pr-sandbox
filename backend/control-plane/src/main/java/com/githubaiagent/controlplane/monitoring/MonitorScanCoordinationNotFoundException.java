package com.githubaiagent.controlplane.monitoring;

public class MonitorScanCoordinationNotFoundException extends RuntimeException {

    public MonitorScanCoordinationNotFoundException(String scannerKey) {
        super("monitor scan coordination not found: " + scannerKey);
    }
}
