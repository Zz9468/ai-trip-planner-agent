"""Async trip planning jobs with optional Redis persistence and SSE events."""

import asyncio
import json
import time
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..agents.trip_planner_agent import get_trip_planner_agent
from ..config import get_settings
from ..models.schemas import TripPlan, TripRequest
from .checkpoint_service import get_checkpoint_service
from .redis_compat import create_redis_client, set_hash_fields

TERMINAL_EVENT_TYPES = {"done", "failed"}
TERMINAL_STATUSES = {"succeeded", "failed"}


class TripJobEvent(BaseModel):
    """One event emitted while a trip planning job is running."""

    type: str = "progress"
    agent: str = ""
    status: str = "info"
    message: str
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    payload: Dict[str, Any] = Field(default_factory=dict)
    sequence: int = 0


class TripJobSnapshot(BaseModel):
    """Public job status returned by the API."""

    job_id: str
    status: str
    result: Optional[TripPlan] = None
    error: Optional[str] = None
    events: List[TripJobEvent] = Field(default_factory=list)
    created_at: str
    updated_at: str


class TripJobRecord:
    """Internal mutable job state for the in-memory backend."""

    def __init__(self, job_id: str, request: TripRequest, loop: asyncio.AbstractEventLoop):
        now = datetime.now().isoformat()
        self.job_id = job_id
        self.request = request
        self.loop = loop
        self.status = "pending"
        self.result: Optional[TripPlan] = None
        self.error: Optional[str] = None
        self.events: List[TripJobEvent] = []
        self.changed = asyncio.Event()
        self.created_at = now
        self.updated_at = now

    def snapshot(self) -> TripJobSnapshot:
        return TripJobSnapshot(
            job_id=self.job_id,
            status=self.status,
            result=self.result,
            error=self.error,
            events=list(self.events),
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


class MemoryTripJobService:
    """Small in-process job manager for local/demo use."""

    def __init__(self):
        settings = get_settings()
        self.heartbeat_seconds = max(int(settings.trip_job_sse_heartbeat_seconds), 5)
        self._jobs: Dict[str, TripJobRecord] = {}
        print("[OK] 异步任务后端: memory")

    def create_job(self, request: TripRequest) -> TripJobSnapshot:
        loop = asyncio.get_running_loop()
        job_id = uuid.uuid4().hex
        record = TripJobRecord(job_id=job_id, request=request, loop=loop)
        self._jobs[job_id] = record

        self._publish_event(
            record,
            TripJobEvent(
                type="progress",
                agent="TripJob",
                status="created",
                message="旅行规划任务已创建",
            ),
        )
        asyncio.create_task(self._run_job(record))
        return record.snapshot()

    def get_job(self, job_id: str) -> Optional[TripJobSnapshot]:
        record = self._jobs.get(job_id)
        if not record:
            return None
        return record.snapshot()

    def create_replay_job(self, source_job_id: str, checkpoint_id: Optional[str] = None) -> TripJobSnapshot:
        source = self._jobs.get(source_job_id)
        if not source:
            raise ValueError("源任务不存在")

        checkpoint_service = get_checkpoint_service()
        if not checkpoint_service.enabled:
            raise ValueError("LangGraph Checkpoint未启用")

        selected_checkpoint_id = checkpoint_id or checkpoint_service.get_latest_checkpoint_id(source_job_id)
        if not selected_checkpoint_id:
            raise ValueError("源任务没有可恢复的checkpoint")
        if not checkpoint_service.checkpoint_exists(source_job_id, selected_checkpoint_id):
            raise ValueError("指定checkpoint不存在")

        request = checkpoint_service.get_request_from_checkpoint(source_job_id, selected_checkpoint_id) or source.request
        loop = asyncio.get_running_loop()
        job_id = uuid.uuid4().hex
        checkpoint_service.copy_thread(source_job_id, job_id)

        record = TripJobRecord(job_id=job_id, request=request, loop=loop)
        self._jobs[job_id] = record
        self._publish_event(
            record,
            TripJobEvent(
                type="progress",
                agent="TripJob",
                status="replay_created",
                message="Checkpoint恢复任务已创建",
                payload={"source_job_id": source_job_id, "checkpoint_id": selected_checkpoint_id},
            ),
        )
        asyncio.create_task(self._run_replay_job(record, selected_checkpoint_id, source_job_id))
        return record.snapshot()

    async def stream_events(self, job_id: str):
        record = self._jobs.get(job_id)
        if not record:
            yield self._format_sse(
                TripJobEvent(
                    type="failed",
                    agent="TripJob",
                    status="not_found",
                    message="任务不存在",
                )
            )
            return

        index = 0
        while True:
            while index < len(record.events):
                event = record.events[index]
                index += 1
                yield self._format_sse(event)

            if record.status in TERMINAL_STATUSES:
                break

            try:
                await asyncio.wait_for(record.changed.wait(), timeout=self.heartbeat_seconds)
                record.changed.clear()
            except asyncio.TimeoutError:
                yield self._format_sse_comment("heartbeat")

    async def _run_job(self, record: TripJobRecord) -> None:
        self._set_status(record, "running")
        self._emit(record, agent="TripJob", status="running", message="后台任务开始执行")

        def event_sink(
            agent: str,
            status: str,
            message: str,
            payload: Optional[Dict[str, Any]] = None,
            event_type: str = "progress",
        ) -> None:
            self.emit_threadsafe(
                record.job_id,
                agent=agent,
                status=status,
                message=message,
                payload=payload,
                event_type=event_type,
            )

        try:
            agent = get_trip_planner_agent()
            plan = await asyncio.to_thread(agent.plan_trip, record.request, event_sink, record.job_id)
            record.result = plan
            self._set_status(record, "succeeded")
            self._emit(
                record,
                agent="TripJob",
                status="succeeded",
                message="旅行计划生成成功",
                payload={"city": plan.city, "days": len(plan.days)},
                event_type="done",
            )
        except Exception as exc:
            record.error = str(exc)
            print(f"[ERROR] 异步旅行规划任务失败: {exc}")
            traceback.print_exc()
            self._set_status(record, "failed")
            self._emit(
                record,
                agent="TripJob",
                status="failed",
                message=f"旅行计划生成失败: {exc}",
                event_type="failed",
            )

    async def _run_replay_job(
        self,
        record: TripJobRecord,
        checkpoint_id: str,
        source_job_id: str,
    ) -> None:
        self._set_status(record, "running")
        self._emit(
            record,
            agent="TripJob",
            status="running",
            message="Checkpoint恢复任务开始执行",
            payload={"source_job_id": source_job_id, "checkpoint_id": checkpoint_id},
        )

        def event_sink(
            agent: str,
            status: str,
            message: str,
            payload: Optional[Dict[str, Any]] = None,
            event_type: str = "progress",
        ) -> None:
            self.emit_threadsafe(
                record.job_id,
                agent=agent,
                status=status,
                message=message,
                payload=payload,
                event_type=event_type,
            )

        try:
            agent = get_trip_planner_agent()
            plan = await asyncio.to_thread(
                agent.replay_trip_from_checkpoint,
                record.request,
                record.job_id,
                checkpoint_id,
                event_sink,
            )
            record.result = plan
            self._set_status(record, "succeeded")
            self._emit(
                record,
                agent="TripJob",
                status="succeeded",
                message="Checkpoint恢复任务执行成功",
                payload={"city": plan.city, "days": len(plan.days)},
                event_type="done",
            )
        except Exception as exc:
            record.error = str(exc)
            print(f"[ERROR] Checkpoint恢复任务失败: {exc}")
            traceback.print_exc()
            self._set_status(record, "failed")
            self._emit(
                record,
                agent="TripJob",
                status="failed",
                message=f"Checkpoint恢复任务失败: {exc}",
                event_type="failed",
            )

    def emit_threadsafe(
        self,
        job_id: str,
        agent: str,
        status: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
        event_type: str = "progress",
    ) -> None:
        record = self._jobs.get(job_id)
        if not record:
            return

        event = TripJobEvent(
            type=event_type,
            agent=agent,
            status=status,
            message=message,
            payload=payload or {},
        )
        record.loop.call_soon_threadsafe(self._publish_event, record, event)

    def _emit(
        self,
        record: TripJobRecord,
        agent: str,
        status: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
        event_type: str = "progress",
    ) -> None:
        self._publish_event(
            record,
            TripJobEvent(
                type=event_type,
                agent=agent,
                status=status,
                message=message,
                payload=payload or {},
            ),
        )

    def _set_status(self, record: TripJobRecord, status: str) -> None:
        record.status = status
        record.updated_at = datetime.now().isoformat()

    @staticmethod
    def _publish_event(record: TripJobRecord, event: TripJobEvent) -> None:
        event.sequence = len(record.events) + 1
        record.events.append(event)
        record.updated_at = datetime.now().isoformat()
        record.changed.set()

    @staticmethod
    def _format_sse(event: TripJobEvent) -> str:
        data = json.dumps(event.model_dump(), ensure_ascii=False)
        return f"event: {event.type}\ndata: {data}\n\n"

    @staticmethod
    def _format_sse_comment(message: str) -> str:
        return f": {message} {datetime.now().isoformat()}\n\n"


class RedisTripJobService:
    """Redis-backed job manager for cross-refresh task state and events."""

    def __init__(self):
        settings = get_settings()
        self.redis_url = settings.redis_url
        self.key_prefix = settings.redis_key_prefix.strip(":") or "trip"
        self.ttl_seconds = max(int(settings.trip_job_ttl_seconds), 60)
        self.heartbeat_seconds = max(int(settings.trip_job_sse_heartbeat_seconds), 5)

        self._redis = create_redis_client(
            self.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=3,
        )
        self._redis.ping()
        print(f"[OK] 异步任务后端: redis ({self.redis_url})")

    def create_job(self, request: TripRequest) -> TripJobSnapshot:
        job_id = uuid.uuid4().hex
        now = datetime.now().isoformat()
        set_hash_fields(
            self._redis,
            self._job_key(job_id),
            {
                "job_id": job_id,
                "status": "pending",
                "request_json": request.model_dump_json(),
                "result_json": "",
                "error": "",
                "created_at": now,
                "updated_at": now,
            },
        )
        self._expire_job(job_id)
        self._emit(
            job_id,
            agent="TripJob",
            status="created",
            message="旅行规划任务已创建",
        )
        asyncio.create_task(self._run_job(job_id, request))

        snapshot = self.get_job(job_id)
        if snapshot:
            return snapshot

        return TripJobSnapshot(job_id=job_id, status="pending", created_at=now, updated_at=now)

    def get_job(self, job_id: str) -> Optional[TripJobSnapshot]:
        data = self._redis.hgetall(self._job_key(job_id))
        if not data:
            return None

        result: Optional[TripPlan] = None
        result_json = data.get("result_json")
        if result_json:
            try:
                result = TripPlan.model_validate_json(result_json)
            except Exception as exc:
                print(f"[WARN] 解析Redis任务结果失败: {exc}")

        return TripJobSnapshot(
            job_id=data.get("job_id") or job_id,
            status=data.get("status") or "unknown",
            result=result,
            error=data.get("error") or None,
            events=self._read_events_from(job_id, 0),
            created_at=data.get("created_at") or "",
            updated_at=data.get("updated_at") or "",
        )

    def create_replay_job(self, source_job_id: str, checkpoint_id: Optional[str] = None) -> TripJobSnapshot:
        source = self.get_job(source_job_id)
        if not source:
            raise ValueError("源任务不存在")

        checkpoint_service = get_checkpoint_service()
        if not checkpoint_service.enabled:
            raise ValueError("LangGraph Checkpoint未启用")

        selected_checkpoint_id = checkpoint_id or checkpoint_service.get_latest_checkpoint_id(source_job_id)
        if not selected_checkpoint_id:
            raise ValueError("源任务没有可恢复的checkpoint")
        if not checkpoint_service.checkpoint_exists(source_job_id, selected_checkpoint_id):
            raise ValueError("指定checkpoint不存在")

        request = checkpoint_service.get_request_from_checkpoint(source_job_id, selected_checkpoint_id)
        if not request:
            raw_request = self._redis.hget(self._job_key(source_job_id), "request_json")
            if not raw_request:
                raise ValueError("无法从源任务恢复请求参数")
            request = TripRequest.model_validate_json(raw_request)

        job_id = uuid.uuid4().hex
        now = datetime.now().isoformat()
        checkpoint_service.copy_thread(source_job_id, job_id)
        set_hash_fields(
            self._redis,
            self._job_key(job_id),
            {
                "job_id": job_id,
                "status": "pending",
                "request_json": request.model_dump_json(),
                "result_json": "",
                "error": "",
                "created_at": now,
                "updated_at": now,
            },
        )
        self._expire_job(job_id)
        self._emit(
            job_id,
            agent="TripJob",
            status="replay_created",
            message="Checkpoint恢复任务已创建",
            payload={"source_job_id": source_job_id, "checkpoint_id": selected_checkpoint_id},
        )
        asyncio.create_task(self._run_replay_job(job_id, request, selected_checkpoint_id, source_job_id))

        snapshot = self.get_job(job_id)
        if snapshot:
            return snapshot
        return TripJobSnapshot(job_id=job_id, status="pending", created_at=now, updated_at=now)

    async def stream_events(self, job_id: str):
        if not self.get_job(job_id):
            yield self._format_sse(
                TripJobEvent(
                    type="failed",
                    agent="TripJob",
                    status="not_found",
                    message="任务不存在",
                )
            )
            return

        index = 0
        last_heartbeat = time.monotonic()
        while True:
            events = await asyncio.to_thread(self._read_events_from, job_id, index)
            for event in events:
                index += 1
                last_heartbeat = time.monotonic()
                yield self._format_sse(event)

            snapshot = await asyncio.to_thread(self.get_job, job_id)
            if not snapshot:
                yield self._format_sse(
                    TripJobEvent(
                        type="failed",
                        agent="TripJob",
                        status="not_found",
                        message="任务不存在",
                    )
                )
                break

            final_event_seen = bool(snapshot.events and snapshot.events[-1].type in TERMINAL_EVENT_TYPES)
            if snapshot.status in TERMINAL_STATUSES and index >= len(snapshot.events) and final_event_seen:
                break

            if time.monotonic() - last_heartbeat >= self.heartbeat_seconds:
                yield self._format_sse_comment("heartbeat")
                last_heartbeat = time.monotonic()

            await asyncio.sleep(1)

    async def _run_job(self, job_id: str, request: TripRequest) -> None:
        self._set_status(job_id, "running")
        self._emit(job_id, agent="TripJob", status="running", message="后台任务开始执行")

        def event_sink(
            agent: str,
            status: str,
            message: str,
            payload: Optional[Dict[str, Any]] = None,
            event_type: str = "progress",
        ) -> None:
            self.emit_threadsafe(
                job_id,
                agent=agent,
                status=status,
                message=message,
                payload=payload,
                event_type=event_type,
            )

        try:
            agent = get_trip_planner_agent()
            plan = await asyncio.to_thread(agent.plan_trip, request, event_sink, job_id)
            self._set_status(job_id, "succeeded", result=plan)
            self._emit(
                job_id,
                agent="TripJob",
                status="succeeded",
                message="旅行计划生成成功",
                payload={"city": plan.city, "days": len(plan.days)},
                event_type="done",
            )
        except Exception as exc:
            print(f"[ERROR] 异步旅行规划任务失败: {exc}")
            traceback.print_exc()
            self._set_status(job_id, "failed", error=str(exc))
            self._emit(
                job_id,
                agent="TripJob",
                status="failed",
                message=f"旅行计划生成失败: {exc}",
                event_type="failed",
            )

    async def _run_replay_job(
        self,
        job_id: str,
        request: TripRequest,
        checkpoint_id: str,
        source_job_id: str,
    ) -> None:
        self._set_status(job_id, "running")
        self._emit(
            job_id,
            agent="TripJob",
            status="running",
            message="Checkpoint恢复任务开始执行",
            payload={"source_job_id": source_job_id, "checkpoint_id": checkpoint_id},
        )

        def event_sink(
            agent: str,
            status: str,
            message: str,
            payload: Optional[Dict[str, Any]] = None,
            event_type: str = "progress",
        ) -> None:
            self.emit_threadsafe(
                job_id,
                agent=agent,
                status=status,
                message=message,
                payload=payload,
                event_type=event_type,
            )

        try:
            agent = get_trip_planner_agent()
            plan = await asyncio.to_thread(
                agent.replay_trip_from_checkpoint,
                request,
                job_id,
                checkpoint_id,
                event_sink,
            )
            self._set_status(job_id, "succeeded", result=plan)
            self._emit(
                job_id,
                agent="TripJob",
                status="succeeded",
                message="Checkpoint恢复任务执行成功",
                payload={"city": plan.city, "days": len(plan.days)},
                event_type="done",
            )
        except Exception as exc:
            print(f"[ERROR] Checkpoint恢复任务失败: {exc}")
            traceback.print_exc()
            self._set_status(job_id, "failed", error=str(exc))
            self._emit(
                job_id,
                agent="TripJob",
                status="failed",
                message=f"Checkpoint恢复任务失败: {exc}",
                event_type="failed",
            )

    def emit_threadsafe(
        self,
        job_id: str,
        agent: str,
        status: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
        event_type: str = "progress",
    ) -> None:
        self._emit(
            job_id,
            agent=agent,
            status=status,
            message=message,
            payload=payload,
            event_type=event_type,
        )

    def _emit(
        self,
        job_id: str,
        agent: str,
        status: str,
        message: str,
        payload: Optional[Dict[str, Any]] = None,
        event_type: str = "progress",
    ) -> None:
        self._publish_event(
            job_id,
            TripJobEvent(
                type=event_type,
                agent=agent,
                status=status,
                message=message,
                payload=payload or {},
            ),
        )

    def _set_status(
        self,
        job_id: str,
        status: str,
        result: Optional[TripPlan] = None,
        error: Optional[str] = None,
    ) -> None:
        mapping = {
            "status": status,
            "updated_at": datetime.now().isoformat(),
        }
        if result is not None:
            mapping["result_json"] = result.model_dump_json()
        if error is not None:
            mapping["error"] = error

        set_hash_fields(self._redis, self._job_key(job_id), mapping)
        self._expire_job(job_id)

    def _publish_event(self, job_id: str, event: TripJobEvent) -> None:
        event.sequence = int(self._redis.incr(self._sequence_key(job_id)))
        self._redis.rpush(self._events_key(job_id), event.model_dump_json())
        set_hash_fields(self._redis, self._job_key(job_id), {"updated_at": datetime.now().isoformat()})
        self._expire_job(job_id)

    def _read_events_from(self, job_id: str, start_index: int) -> List[TripJobEvent]:
        raw_events = self._redis.lrange(self._events_key(job_id), start_index, -1)
        events: List[TripJobEvent] = []
        for raw in raw_events:
            try:
                events.append(TripJobEvent.model_validate_json(raw))
            except Exception as exc:
                print(f"[WARN] 解析Redis任务事件失败: {exc}")
        return events

    def _expire_job(self, job_id: str) -> None:
        self._redis.expire(self._job_key(job_id), self.ttl_seconds)
        self._redis.expire(self._events_key(job_id), self.ttl_seconds)
        self._redis.expire(self._sequence_key(job_id), self.ttl_seconds)

    def _job_key(self, job_id: str) -> str:
        return f"{self.key_prefix}:job:{job_id}"

    def _events_key(self, job_id: str) -> str:
        return f"{self.key_prefix}:job:{job_id}:events"

    def _sequence_key(self, job_id: str) -> str:
        return f"{self.key_prefix}:job:{job_id}:sequence"

    @staticmethod
    def _format_sse(event: TripJobEvent) -> str:
        data = json.dumps(event.model_dump(), ensure_ascii=False)
        return f"event: {event.type}\ndata: {data}\n\n"

    @staticmethod
    def _format_sse_comment(message: str) -> str:
        return f": {message} {datetime.now().isoformat()}\n\n"


TripJobService = MemoryTripJobService

_trip_job_service: Optional[Any] = None


def get_trip_job_service() -> Any:
    """Get the shared trip job service."""
    global _trip_job_service

    if _trip_job_service is not None:
        return _trip_job_service

    settings = get_settings()
    backend = settings.trip_job_backend.strip().lower()
    if backend not in {"auto", "redis", "memory"}:
        print(f"[WARN] 未识别的TRIP_JOB_BACKEND={settings.trip_job_backend},使用auto")
        backend = "auto"

    if backend in {"auto", "redis"}:
        try:
            _trip_job_service = RedisTripJobService()
            return _trip_job_service
        except Exception as exc:
            if backend == "redis":
                raise RuntimeError(f"Redis任务后端不可用: {exc}") from exc
            print(f"[WARN] Redis任务后端不可用,自动回退到memory: {exc}")

    _trip_job_service = MemoryTripJobService()
    return _trip_job_service
