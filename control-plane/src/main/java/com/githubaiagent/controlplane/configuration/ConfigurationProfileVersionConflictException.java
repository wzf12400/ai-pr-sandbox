package com.githubaiagent.controlplane.configuration;

public class ConfigurationProfileVersionConflictException extends RuntimeException {

    public ConfigurationProfileVersionConflictException(long expectedVersion, long currentVersion) {
        super("Configuration profile version conflict; expected " + expectedVersion
                + " but current version is " + currentVersion + ". Refresh and retry.");
    }
}
