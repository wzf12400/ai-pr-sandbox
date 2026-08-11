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
    @Query("""
            select job from AutomationJob job
            where job.status = :status
              and not exists (
                  select outbox.taskId from TaskQueueOutbox outbox
                  where outbox.taskId = job.id
              )
            order by job.createdAt asc
            """)
    List<AutomationJob> findWithoutQueueOutbox(
            @Param("status") TaskStatus status,
            Pageable pageable
    );

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("""
            select job from AutomationJob job, TaskQueueOutbox outbox
            where job.id = outbox.taskId
              and job.status = :status
              and outbox.publishedGeneration = outbox.generation
              and outbox.publishedAt is not null
              and outbox.publishedAt < :cutoff
            order by outbox.publishedAt asc
            """)
    List<AutomationJob> findWithStalePublishedQueueOutbox(
            @Param("status") TaskStatus status,
            @Param("cutoff") java.time.Instant cutoff,
            Pageable pageable
    );

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    List<AutomationJob> findAllByStatusAndUpdatedAtBeforeOrderByUpdatedAtAsc(
            TaskStatus status,
            java.time.Instant cutoff,
            Pageable pageable
    );

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select job from AutomationJob job where job.id = :taskId")
    Optional<AutomationJob> findByIdForUpdate(@Param("taskId") String taskId);
}
