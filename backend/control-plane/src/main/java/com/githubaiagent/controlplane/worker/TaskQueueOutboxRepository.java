package com.githubaiagent.controlplane.worker;

import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;

import jakarta.persistence.LockModeType;

import java.util.List;

public interface TaskQueueOutboxRepository extends JpaRepository<TaskQueueOutbox, String> {

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("""
            select outbox from TaskQueueOutbox outbox
            where outbox.publishedGeneration < outbox.generation
            order by outbox.updatedAt asc
            """)
    List<TaskQueueOutbox> findPending(Pageable pageable);
}
