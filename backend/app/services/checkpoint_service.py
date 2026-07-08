"""LangGraph checkpoint lifecycle and read APIs."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from ..config import get_settings
from ..models.schemas import Attraction, Hotel, Meal, TripPlan, TripRequest, WeatherInfo


class TripCheckpointSummary(BaseModel):
    """Public summary of a persisted LangGraph checkpoint."""

    checkpoint_id: str
    parent_checkpoint_id: Optional[str] = None
    thread_id: str
    step: int
    source: str = ""
    created_at: str = ""
    stage: str = "unknown"
    updated_channels: List[str] = Field(default_factory=list)
    summary: Dict[str, Any] = Field(default_factory=dict)


class LangGraphCheckpointService:
    """Own the checkpointer context and expose checkpoint summaries."""

    def __init__(self):
        self.settings = get_settings()
        self.enabled = bool(self.settings.langgraph_checkpoint_enabled)
        self.backend = self.settings.langgraph_checkpoint_backend.strip().lower()
        self.db_path = self._resolve_db_path(self.settings.langgraph_checkpoint_db)
        self._context: Optional[AbstractContextManager[Any]] = None
        self._checkpointer: Optional[Any] = None

        if not self.enabled:
            print("[OK] LangGraph Checkpoint disabled")
            return

        if self.backend != "sqlite":
            print(f"[WARN] Unsupported LangGraph checkpoint backend: {self.backend}; disabled")
            self.enabled = False
            return

        try:
            from langgraph.checkpoint.sqlite import SqliteSaver

            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._context = SqliteSaver.from_conn_string(str(self.db_path))
            checkpointer = self._context.__enter__()
            self._checkpointer = self._with_app_allowlist(checkpointer)
            print(f"[OK] LangGraph SQLite checkpoint enabled: {self.db_path}")
        except Exception as exc:
            print(f"[WARN] LangGraph SQLite checkpoint unavailable, disabled: {exc}")
            self.enabled = False
            self._checkpointer = None
            self._context = None

    @property
    def checkpointer(self) -> Optional[Any]:
        return self._checkpointer if self.enabled else None

    def close(self) -> None:
        if not self._context:
            return
        try:
            self._context.__exit__(None, None, None)
            print("[OK] LangGraph checkpoint closed")
        except Exception as exc:
            print(f"[WARN] Close LangGraph checkpoint failed: {exc}")
        finally:
            self._context = None
            self._checkpointer = None

    def list_checkpoints(self, thread_id: str, limit: int = 50) -> List[TripCheckpointSummary]:
        if not self.checkpointer:
            return []

        config = {"configurable": {"thread_id": thread_id}}
        checkpoints = list(self.checkpointer.list(config, limit=limit))
        summaries = [self._to_summary(item) for item in checkpoints]
        summaries.sort(key=lambda item: item.step)
        return summaries

    def get_checkpoint_config(self, thread_id: str, checkpoint_id: str) -> Dict[str, Any]:
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": "",
                "checkpoint_id": checkpoint_id,
            }
        }

    def checkpoint_exists(self, thread_id: str, checkpoint_id: str) -> bool:
        if not self.checkpointer:
            return False
        return self.checkpointer.get_tuple(self.get_checkpoint_config(thread_id, checkpoint_id)) is not None

    def get_latest_checkpoint_id(self, thread_id: str) -> Optional[str]:
        summaries = self.list_checkpoints(thread_id, limit=1)
        if not summaries:
            return None
        return summaries[-1].checkpoint_id

    def get_request_from_checkpoint(self, thread_id: str, checkpoint_id: str) -> Optional[TripRequest]:
        if not self.checkpointer:
            return None

        item = self.checkpointer.get_tuple(self.get_checkpoint_config(thread_id, checkpoint_id))
        if not item:
            return None

        values = (item.checkpoint or {}).get("channel_values") or {}
        request = values.get("request")
        if not request and isinstance(values.get("__start__"), dict):
            request = values["__start__"].get("request")
        return request if isinstance(request, TripRequest) else None

    def copy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        """Copy persisted checkpoints into a fresh target thread id.

        LangGraph exposes copy_thread on the base saver, but the SQLite saver in
        this version does not implement it. We copy the two SQLite tables
        directly so replay jobs get their own job id and checkpoint history.
        """
        if not self.checkpointer:
            raise RuntimeError("LangGraph Checkpoint未启用")
        if source_thread_id == target_thread_id:
            return

        with sqlite3.connect(self.db_path) as conn:
            source_count = conn.execute(
                "select count(*) from checkpoints where thread_id = ?",
                (source_thread_id,),
            ).fetchone()[0]
            if not source_count:
                raise ValueError("源任务没有可复制的checkpoint")

            conn.execute("delete from checkpoints where thread_id = ?", (target_thread_id,))
            conn.execute("delete from writes where thread_id = ?", (target_thread_id,))
            conn.execute(
                """
                insert into checkpoints(
                    thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                    type, checkpoint, metadata
                )
                select ?, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                       type, checkpoint, metadata
                from checkpoints
                where thread_id = ?
                """,
                (target_thread_id, source_thread_id),
            )
            conn.execute(
                """
                insert into writes(
                    thread_id, checkpoint_ns, checkpoint_id, task_id,
                    idx, channel, type, value
                )
                select ?, checkpoint_ns, checkpoint_id, task_id,
                       idx, channel, type, value
                from writes
                where thread_id = ?
                """,
                (target_thread_id, source_thread_id),
            )
            conn.commit()

    @staticmethod
    def _resolve_db_path(raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            return path
        backend_dir = Path(__file__).resolve().parents[2]
        return (backend_dir / path).resolve()

    @staticmethod
    def _with_app_allowlist(checkpointer: Any) -> Any:
        allowlist = [
            ("app.models.schemas", "TripRequest"),
            ("app.models.schemas", "POIInfo"),
            ("app.models.schemas", "Location"),
            ("app.models.schemas", "Attraction"),
            ("app.models.schemas", "Hotel"),
            ("app.models.schemas", "Meal"),
            ("app.models.schemas", "WeatherInfo"),
            ("app.models.schemas", "Budget"),
            ("app.models.schemas", "DayPlan"),
            ("app.models.schemas", "TripPlan"),
            ("app.agents.trip_planner_agent", "DayRoute"),
            ("app.agents.trip_planner_agent", "RoutePlan"),
            ("app.agents.trip_planner_agent", "ReviewIssue"),
            ("app.agents.trip_planner_agent", "ReviewReport"),
        ]
        if hasattr(checkpointer, "with_allowlist"):
            return checkpointer.with_allowlist(allowlist)
        return checkpointer

    def _to_summary(self, item: Any) -> TripCheckpointSummary:
        config = item.config.get("configurable", {})
        parent_config = (item.parent_config or {}).get("configurable", {})
        checkpoint = item.checkpoint or {}
        values = checkpoint.get("channel_values") or {}
        metadata = item.metadata or {}

        return TripCheckpointSummary(
            checkpoint_id=config.get("checkpoint_id", ""),
            parent_checkpoint_id=parent_config.get("checkpoint_id"),
            thread_id=config.get("thread_id", ""),
            step=int(metadata.get("step", 0)),
            source=str(metadata.get("source", "")),
            created_at=str(checkpoint.get("ts", "")),
            stage=self._infer_stage(values),
            updated_channels=[str(item) for item in (checkpoint.get("updated_channels") or [])],
            summary=self._build_state_summary(values),
        )

    @staticmethod
    def _infer_stage(values: Dict[str, Any]) -> str:
        if values.get("review") is not None:
            return "critique"
        if values.get("plan") is not None:
            return "plan"
        if values.get("route_plan") is not None:
            return "route"
        if values.get("attractions") is not None or values.get("weather_info") is not None:
            return "research"
        if values.get("request") is not None or values.get("__start__") is not None:
            return "input"
        return "unknown"

    @staticmethod
    def _build_state_summary(values: Dict[str, Any]) -> Dict[str, Any]:
        request = values.get("request")
        if not request and isinstance(values.get("__start__"), dict):
            request = values["__start__"].get("request")

        attractions = values.get("attractions") or []
        hotels = values.get("hotels") or []
        restaurants = values.get("restaurants") or []
        weather = values.get("weather_info") or []
        route_plan = values.get("route_plan")
        plan = values.get("plan")
        review = values.get("review")
        errors = values.get("errors") or []

        summary: Dict[str, Any] = {}
        if request:
            summary["request"] = {
                "city": getattr(request, "city", ""),
                "start_date": getattr(request, "start_date", ""),
                "end_date": getattr(request, "end_date", ""),
                "travel_days": getattr(request, "travel_days", 0),
            }
        summary["counts"] = {
            "attractions": len(attractions),
            "hotels": len(hotels),
            "restaurants": len(restaurants),
            "weather_days": len(weather),
        }

        if attractions:
            summary["sample_attractions"] = [item.name for item in attractions[:5] if isinstance(item, Attraction)]
        if hotels:
            summary["sample_hotels"] = [item.name for item in hotels[:3] if isinstance(item, Hotel)]
        if restaurants:
            summary["sample_restaurants"] = [item.name for item in restaurants[:3] if isinstance(item, Meal)]
        if weather:
            summary["sample_weather_dates"] = [item.date for item in weather[:3] if isinstance(item, WeatherInfo)]
        if route_plan:
            day_routes = getattr(route_plan, "day_routes", []) or []
            summary["route_days"] = len(day_routes)
            summary["route_warnings"] = getattr(route_plan, "warnings", []) or []
        if isinstance(plan, TripPlan):
            summary["plan"] = {
                "city": plan.city,
                "days": len(plan.days),
                "budget_total": plan.budget.total if plan.budget else 0,
            }
        if review:
            issues = getattr(review, "issues", []) or []
            summary["review"] = {
                "passed": bool(getattr(review, "passed", False)),
                "needs_revision": bool(getattr(review, "needs_revision", False)),
                "issue_count": len(issues),
                "high_issue_count": sum(1 for issue in issues if getattr(issue, "level", "") == "high"),
            }
        if errors:
            summary["errors"] = [str(item) for item in errors[-5:]]
        return summary


_checkpoint_service: Optional[LangGraphCheckpointService] = None


def get_checkpoint_service() -> LangGraphCheckpointService:
    """Return the process-wide LangGraph checkpoint service."""
    global _checkpoint_service
    if _checkpoint_service is None:
        _checkpoint_service = LangGraphCheckpointService()
    return _checkpoint_service
