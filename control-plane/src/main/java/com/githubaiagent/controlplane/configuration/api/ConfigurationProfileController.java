package com.githubaiagent.controlplane.configuration.api;

import com.githubaiagent.controlplane.configuration.ConfigurationProfileService;
import jakarta.validation.Valid;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/configuration-profiles")
public class ConfigurationProfileController {

    private final ConfigurationProfileService profileService;

    public ConfigurationProfileController(ConfigurationProfileService profileService) {
        this.profileService = profileService;
    }

    @GetMapping("/{profileKey}")
    public ConfigurationProfileResponse get(@PathVariable String profileKey) {
        return profileService.get(profileKey);
    }

    @PutMapping("/{profileKey}")
    public ConfigurationProfileResponse put(
            @PathVariable String profileKey,
            @Valid @RequestBody PutConfigurationProfileRequest request
    ) {
        return profileService.put(profileKey, request);
    }
}
