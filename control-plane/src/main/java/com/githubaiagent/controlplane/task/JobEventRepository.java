package com.githubaiagent.controlplane.task;

import org.springframework.data.jpa.repository.JpaRepository;

import java.util.List;

public interface JobEventRepository extends JpaRepository<JobEvent, Long> {
    List<JobEvent> findByJobIdOrderByCreatedAtAscIdAsc(String jobId);

    void deleteByJobId(String jobId);
}
