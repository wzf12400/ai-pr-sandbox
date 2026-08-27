package com.githubaiagent.controlplane.configuration;

import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Optional;

public interface ConfigurationProfileRepository extends JpaRepository<ConfigurationProfile, String> {

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("""
            select profile
            from ConfigurationProfile profile
            where profile.profileKey = :profileKey
            """)
    Optional<ConfigurationProfile> findByProfileKeyForUpdate(@Param("profileKey") String profileKey);
}
