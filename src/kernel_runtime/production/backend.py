from __future__ import annotations

import json
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Iterator, Protocol

from kernel_runtime.errors import RuntimeFailure

from .models import JobRecord, utc_now


class JobBackend(Protocol):
    def submit(self, job: JobRecord) -> JobRecord: ...
    def claim(self, worker_id: str, lease_seconds: float, wait_seconds: float) -> JobRecord | None: ...
    def attach_task(self, job_id: str, task_id: str) -> None: ...
    def complete(self, job_id: str, result: dict) -> None: ...
    def fail(self, job_id: str, code: str, message: str) -> None: ...
    def cancel(self, job_id: str) -> bool: ...
    def get(self, job_id: str) -> JobRecord: ...
    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: float) -> bool: ...
    def requeue_expired(self) -> int: ...


class InMemoryJobBackend:
    """本地运行和自动测试使用，语义与分布式后端保持一致。"""

    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._idempotency: dict[tuple[str, str, str], str] = {}
        self._queue: deque[str] = deque()
        self._session_locks: dict[str, threading.Lock] = {}
        self._condition = threading.Condition(threading.RLock())

    def submit(self, job: JobRecord) -> JobRecord:
        key = (job.application_id, job.session_id, job.idempotency_key)
        with self._condition:
            existing_id = self._idempotency.get(key)
            if existing_id:
                return self._jobs[existing_id]
            self._jobs[job.job_id] = job
            self._idempotency[key] = job.job_id
            self._queue.append(job.job_id)
            self._condition.notify()
            return job

    def claim(self, worker_id: str, lease_seconds: float = 30, wait_seconds: float = 1) -> JobRecord | None:
        deadline = time.monotonic() + wait_seconds
        with self._condition:
            while True:
                while self._queue:
                    job = self._jobs[self._queue.popleft()]
                    if job.cancelled or job.status not in {"QUEUED", "RETRY"}:
                        continue
                    job.status = "RUNNING"
                    job.attempt_no += 1
                    job.lease_owner = worker_id
                    job.lease_until = time.time() + lease_seconds
                    job.updated_at = utc_now()
                    return JobRecord.from_dict(job.as_dict())
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def attach_task(self, job_id: str, task_id: str) -> None:
        with self._condition:
            self._jobs[job_id].task_id = task_id
            self._jobs[job_id].updated_at = utc_now()

    def complete(self, job_id: str, result: dict) -> None:
        with self._condition:
            job = self._jobs[job_id]
            if job.cancelled:
                job.status = "CANCELLED"
                job.result = None
            else:
                job.status = "COMPLETED"
                job.result = result
            job.lease_owner = None
            job.lease_until = 0
            job.updated_at = utc_now()

    def fail(self, job_id: str, code: str, message: str) -> None:
        with self._condition:
            job = self._jobs[job_id]
            job.status = "FAILED"
            job.error = {"error_code": code, "message": message}
            job.lease_owner = None
            job.lease_until = 0
            job.updated_at = utc_now()

    def cancel(self, job_id: str) -> bool:
        with self._condition:
            job = self._jobs.get(job_id)
            if not job or job.status in {"COMPLETED", "FAILED", "CANCELLED"}:
                return False
            job.cancelled = True
            job.status = "CANCELLED"
            job.updated_at = utc_now()
            self._condition.notify_all()
            return True

    def get(self, job_id: str) -> JobRecord:
        with self._condition:
            if job_id not in self._jobs:
                raise RuntimeFailure("JOB_NOT_FOUND", job_id)
            return JobRecord.from_dict(self._jobs[job_id].as_dict())

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: float = 30) -> bool:
        with self._condition:
            job = self._jobs.get(job_id)
            if not job or job.status != "RUNNING" or job.lease_owner != worker_id:
                return False
            job.lease_until = time.time() + lease_seconds
            job.updated_at = utc_now()
            return True

    def requeue_expired(self) -> int:
        count = 0
        with self._condition:
            now = time.time()
            for job in self._jobs.values():
                if job.status == "RUNNING" and job.lease_until < now and not job.cancelled:
                    job.status = "RETRY"
                    job.lease_owner = None
                    job.lease_until = 0
                    self._queue.append(job.job_id)
                    count += 1
            if count:
                self._condition.notify_all()
        return count

    @contextmanager
    def session_lock(self, session_id: str, timeout: float = 30) -> Iterator[bool]:
        with self._condition:
            lock = self._session_locks.setdefault(session_id, threading.Lock())
        acquired = lock.acquire(timeout=timeout)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()


class RedisJobBackend:
    """多实例预生产使用。Redis不可用时立即失败，不静默降级。"""

    def __init__(self, url: str, namespace: str = "kernel:v55") -> None:
        try:
            import redis
        except ImportError as exc:
            raise RuntimeFailure("REDIS_SDK_MISSING", "Install redis package") from exc
        self.redis = redis.Redis.from_url(url, decode_responses=True)
        self.namespace = namespace

    def _job_key(self, job_id: str) -> str:
        return f"{self.namespace}:job:{job_id}"

    @property
    def queue_key(self) -> str:
        return f"{self.namespace}:queue"

    def submit(self, job: JobRecord) -> JobRecord:
        idem = f"{self.namespace}:idem:{job.application_id}:{job.session_id}:{job.idempotency_key}"
        existing = self.redis.get(idem)
        if existing:
            return self.get(existing)
        if not self.redis.set(idem, job.job_id, nx=True, ex=86400):
            return self.get(self.redis.get(idem))
        self.redis.set(self._job_key(job.job_id), json.dumps(job.as_dict(), ensure_ascii=False))
        self.redis.rpush(self.queue_key, job.job_id)
        return job

    def _save(self, job: JobRecord) -> None:
        self.redis.set(self._job_key(job.job_id), json.dumps(job.as_dict(), ensure_ascii=False))

    def claim(self, worker_id: str, lease_seconds: float = 30, wait_seconds: float = 1) -> JobRecord | None:
        item = self.redis.blpop(self.queue_key, timeout=max(1, int(wait_seconds)))
        if not item:
            return None
        job = self.get(item[1])
        if job.cancelled or job.status not in {"QUEUED", "RETRY"}:
            return None
        job.status = "RUNNING"
        job.attempt_no += 1
        job.lease_owner = worker_id
        job.lease_until = time.time() + lease_seconds
        job.updated_at = utc_now()
        self._save(job)
        self.redis.zadd(f"{self.namespace}:leases", {job.job_id: job.lease_until})
        return job

    def attach_task(self, job_id: str, task_id: str) -> None:
        job = self.get(job_id); job.task_id = task_id; job.updated_at = utc_now(); self._save(job)

    def complete(self, job_id: str, result: dict) -> None:
        job = self.get(job_id)
        job.status = "CANCELLED" if job.cancelled else "COMPLETED"
        job.result = None if job.cancelled else result
        job.lease_owner = None; job.lease_until = 0; job.updated_at = utc_now(); self._save(job)
        self.redis.zrem(f"{self.namespace}:leases", job_id)

    def fail(self, job_id: str, code: str, message: str) -> None:
        job = self.get(job_id); job.status = "FAILED"
        job.error = {"error_code": code, "message": message}
        job.lease_owner = None; job.lease_until = 0; job.updated_at = utc_now(); self._save(job)
        self.redis.zrem(f"{self.namespace}:leases", job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job.status in {"COMPLETED", "FAILED", "CANCELLED"}: return False
        job.cancelled = True; job.status = "CANCELLED"; job.updated_at = utc_now(); self._save(job); return True

    def get(self, job_id: str) -> JobRecord:
        raw = self.redis.get(self._job_key(job_id))
        if not raw: raise RuntimeFailure("JOB_NOT_FOUND", job_id)
        return JobRecord.from_dict(json.loads(raw))

    def heartbeat(self, job_id: str, worker_id: str, lease_seconds: float = 30) -> bool:
        job = self.get(job_id)
        if job.status != "RUNNING" or job.lease_owner != worker_id: return False
        job.lease_until = time.time() + lease_seconds; job.updated_at = utc_now(); self._save(job)
        self.redis.zadd(f"{self.namespace}:leases", {job.job_id: job.lease_until}); return True

    def requeue_expired(self) -> int:
        ids = self.redis.zrangebyscore(f"{self.namespace}:leases", 0, time.time())
        count = 0
        for job_id in ids:
            job = self.get(job_id)
            if job.status == "RUNNING" and not job.cancelled:
                job.status = "RETRY"; job.lease_owner = None; job.lease_until = 0; self._save(job)
                self.redis.rpush(self.queue_key, job_id); count += 1
            self.redis.zrem(f"{self.namespace}:leases", job_id)
        return count

    @contextmanager
    def session_lock(self, session_id: str, timeout: float = 30):
        lock = self.redis.lock(f"{self.namespace}:session:{session_id}", timeout=timeout + 5)
        acquired = lock.acquire(blocking=True, blocking_timeout=timeout)
        try: yield acquired
        finally:
            if acquired: lock.release()
