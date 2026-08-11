package com.githubaiagent.controlplane.task;

import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import jakarta.persistence.LockModeType;

import java.util.List;
import java.util.Optional;

public interface AutomationJobRepository extends JpaRepository<AutomationJob, String> {
    List<AutomationJob> findAllByOrderByCreatedAtDesc(Pageable pageable);

    Optional<AutomationJob> findFirstBySourceReferenceOrderByCreatedAtAsc(
            String sourceReference
    );

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select job from AutomationJob job where job.id = :taskId")
    Optional<AutomationJob> findByIdForUpdate(@Param("taskId") String taskId);
}
