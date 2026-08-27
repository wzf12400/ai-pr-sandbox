package com.githubaiagent.controlplane.configuration;

public class ConfigurationProfileNotFoundException extends RuntimeException {

    public ConfigurationProfileNotFoundException(String profileKey) {
        super("Configuration profile not found: " + profileKey);
    }
}
