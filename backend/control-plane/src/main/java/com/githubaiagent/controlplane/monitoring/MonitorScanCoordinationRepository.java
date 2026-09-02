package com.githubaiagent.controlplane.monitoring;

import jakarta.persistence.LockModeType;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Optional;

public interface MonitorScanCoordinationRepository
        extends JpaRepository<MonitorScanCoordination, String> {

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("""
            select coordination
            from MonitorScanCoordination coordination
            where coordination.scannerKey = :scannerKey
            """)
    Optional<MonitorScanCoordination> findByScannerKeyForUpdate(
            @Param("scannerKey") String scannerKey
    );
}
