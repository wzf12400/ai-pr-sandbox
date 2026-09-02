package com.githubaiagent.controlplane.configuration;

import com.githubaiagent.controlplane.configuration.api.ConfigurationProfileResponse;
import com.githubaiagent.controlplane.configuration.api.PutConfigurationProfileRequest;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.core.type.TypeReference;
import tools.jackson.databind.ObjectMapper;

import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

@Service
public class ConfigurationProfileService {

    public static final String CONSOLE_SETTINGS = "CONSOLE_SETTINGS";
    private static final Set<String> SUPPORTED_PROFILE_KEYS = Set.of(CONSOLE_SETTINGS);
    private static final int MAX_PAYLOAD_BYTES = 1_000_000;
    private static final Pattern SOURCE_PATTERN = Pattern.compile("[A-Za-z][A-Za-z0-9._-]{0,63}");
    private static final TypeReference<Map<String, Object>> MAP_TYPE = new TypeReference<>() {
    };

    private final ConfigurationProfileRepository profileRepository;
    private final ObjectMapper objectMapper;

    public ConfigurationProfileService(
            ConfigurationProfileRepository profileRepository,
            ObjectMapper objectMapper
    ) {
        this.profileRepository = profileRepository;
        this.objectMapper = objectMapper;
    }

    @Transactional(readOnly = true)
    public ConfigurationProfileResponse get(String profileKey) {
        String supportedProfileKey = requireSupportedProfileKey(profileKey);
        ConfigurationProfile profile = profileRepository.findById(supportedProfileKey)
                .orElseThrow(() -> new ConfigurationProfileNotFoundException(profileKey));
        return response(profile);
    }

    @Transactional
    public ConfigurationProfileResponse put(String profileKey, PutConfigurationProfileRequest request) {
        String supportedProfileKey = requireSupportedProfileKey(profileKey);
        validateRequest(request);
        String payloadJson = serializePayload(request.payload());

        ConfigurationProfile profile = profileRepository.findByProfileKeyForUpdate(supportedProfileKey)
                .orElseThrow(() -> new ConfigurationProfileNotFoundException(profileKey));
        if (profile.getVersion() != request.expectedVersion()) {
            throw new ConfigurationProfileVersionConflictException(
                    request.expectedVersion(), profile.getVersion()
            );
        }

        profile.replaceSnapshot(payloadJson, request.source().trim(), Instant.now());
        ConfigurationProfile updated = profileRepository.saveAndFlush(profile);
        return response(updated);
    }

    private String requireSupportedProfileKey(String profileKey) {
        if (!SUPPORTED_PROFILE_KEYS.contains(profileKey)) {
            throw new ConfigurationProfileNotFoundException(profileKey);
        }
        return profileKey;
    }

    private void validateRequest(PutConfigurationProfileRequest request) {
        if (request == null || request.expectedVersion() == null || request.expectedVersion() < 0) {
            throw new IllegalArgumentException("expectedVersion must be a non-negative number");
        }
        if (request.payload() == null) {
            throw new IllegalArgumentException("payload must be a JSON object");
        }
        String source = request.source() == null ? "" : request.source().trim();
        if (!SOURCE_PATTERN.matcher(source).matches()) {
            throw new IllegalArgumentException("source must be a 1-64 character safe identifier");
        }
    }

    private String serializePayload(Map<String, Object> payload) {
        try {
            String serialized = objectMapper.writeValueAsString(payload);
            if (serialized.getBytes(StandardCharsets.UTF_8).length > MAX_PAYLOAD_BYTES) {
                throw new IllegalArgumentException("payload must not exceed 1000000 UTF-8 bytes");
            }
            return serialized;
        } catch (IllegalArgumentException exception) {
            throw exception;
        } catch (Exception exception) {
            throw new IllegalArgumentException("payload cannot be serialized", exception);
        }
    }

    private ConfigurationProfileResponse response(ConfigurationProfile profile) {
        try {
            return new ConfigurationProfileResponse(
                    profile.getProfileKey(),
                    payloadFrom(profile.getPayloadJson()),
                    profile.isInitialized(),
                    profile.getSource(),
                    profile.getVersion(),
                    profile.getUpdatedAt()
            );
        } catch (Exception exception) {
            throw new IllegalStateException("stored configuration profile payload is not a JSON object", exception);
        }
    }

    private Map<String, Object> payloadFrom(String payloadJson) throws Exception {
        try {
            return objectMapper.readValue(payloadJson, MAP_TYPE);
        } catch (Exception directParseFailure) {
            // H2 represents values bound through a VARCHAR parameter to a JSON
            // column as a JSON string. MySQL returns the object document directly.
            String h2WrappedJson = objectMapper.readValue(payloadJson, String.class);
            return objectMapper.readValue(h2WrappedJson, MAP_TYPE);
        }
    }
}
