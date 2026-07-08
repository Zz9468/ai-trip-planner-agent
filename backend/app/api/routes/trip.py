"""旅行规划API路由"""

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import List, Optional
from ...models.schemas import (
    TripRequest,
    TripPlan,
    TripPlanResponse,
    ErrorResponse
)
from ...agents.trip_planner_agent import get_trip_planner_agent
from ...services.checkpoint_service import TripCheckpointSummary, get_checkpoint_service
from ...services.trip_job_service import TripJobEvent, get_trip_job_service

router = APIRouter(prefix="/trip", tags=["旅行规划"])


class TripJobCreateResponse(BaseModel):
    """旅行规划异步任务创建响应"""

    success: bool = True
    message: str = ""
    job_id: str
    status: str


class TripJobStatusResponse(BaseModel):
    """旅行规划异步任务状态响应"""

    success: bool = True
    message: str = ""
    job_id: str
    status: str
    data: Optional[TripPlan] = None
    error: Optional[str] = None
    events: List[TripJobEvent] = Field(default_factory=list)


class TripJobCheckpointsResponse(BaseModel):
    """LangGraph checkpoint summaries for one trip job."""

    success: bool = True
    message: str = ""
    job_id: str
    enabled: bool = True
    checkpoints: List[TripCheckpointSummary] = Field(default_factory=list)


class TripJobReplayRequest(BaseModel):
    """Create a new job by replaying/resuming from a checkpoint."""

    checkpoint_id: Optional[str] = Field(default=None, description="不传则使用该任务最新checkpoint")


class TripJobReplayResponse(BaseModel):
    """Replay/resume job creation response."""

    success: bool = True
    message: str = ""
    source_job_id: str
    checkpoint_id: Optional[str] = None
    job_id: str
    status: str


@router.post(
    "/plan",
    response_model=TripPlanResponse,
    summary="生成旅行计划",
    description="根据用户输入的旅行需求,生成详细的旅行计划"
)
async def plan_trip(request: TripRequest):
    """
    生成旅行计划

    Args:
        request: 旅行请求参数

    Returns:
        旅行计划响应
    """
    try:
        print(f"\n{'='*60}")
        print("收到旅行规划请求:")
        print(f"   城市: {request.city}")
        print(f"   日期: {request.start_date} - {request.end_date}")
        print(f"   天数: {request.travel_days}")
        print(f"{'='*60}\n")

        # 获取规划工作流实例
        print("获取 LangGraph 旅行规划工作流实例...")
        agent = get_trip_planner_agent()

        # 生成旅行计划
        print("开始生成旅行计划...")
        trip_plan = agent.plan_trip(request)

        print("[OK] 旅行计划生成成功,准备返回响应\n")

        return TripPlanResponse(
            success=True,
            message="旅行计划生成成功",
            data=trip_plan
        )

    except Exception as e:
        print(f"[ERROR] 生成旅行计划失败: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"生成旅行计划失败: {str(e)}"
        )


@router.post(
    "/jobs",
    response_model=TripJobCreateResponse,
    summary="创建异步旅行规划任务",
    description="创建后台旅行规划任务,通过 SSE 接口实时监听多Agent执行进度"
)
async def create_trip_job(request: TripRequest):
    """创建异步旅行规划任务"""
    try:
        print(f"\n{'='*60}")
        print("创建异步旅行规划任务:")
        print(f"   城市: {request.city}")
        print(f"   日期: {request.start_date} - {request.end_date}")
        print(f"   天数: {request.travel_days}")
        print(f"{'='*60}\n")

        service = get_trip_job_service()
        job = service.create_job(request)
        return TripJobCreateResponse(
            success=True,
            message="旅行规划任务已创建",
            job_id=job.job_id,
            status=job.status,
        )
    except Exception as e:
        print(f"[ERROR] 创建旅行规划任务失败: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"创建旅行规划任务失败: {str(e)}"
        )


@router.get(
    "/jobs/{job_id}",
    response_model=TripJobStatusResponse,
    summary="获取异步旅行规划任务结果",
    description="查询任务状态,成功后返回 TripPlan"
)
async def get_trip_job(job_id: str):
    """获取异步任务状态和结果"""
    service = get_trip_job_service()
    job = service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")

    return TripJobStatusResponse(
        success=job.status != "failed",
        message="任务查询成功" if job.status != "failed" else (job.error or "任务失败"),
        job_id=job.job_id,
        status=job.status,
        data=job.result,
        error=job.error,
        events=job.events,
    )


@router.get(
    "/jobs/{job_id}/events",
    summary="监听异步旅行规划任务进度",
    description="通过 Server-Sent Events 实时接收多Agent执行进度"
)
async def stream_trip_job_events(job_id: str):
    """SSE 事件流"""
    service = get_trip_job_service()
    if not service.get_job(job_id):
        raise HTTPException(status_code=404, detail="任务不存在")

    return StreamingResponse(
        service.stream_events(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/jobs/{job_id}/checkpoints",
    response_model=TripJobCheckpointsResponse,
    summary="查询旅行规划任务的 LangGraph Checkpoint",
    description="返回某个异步任务在 LangGraph 工作流中的持久化状态快照摘要"
)
async def get_trip_job_checkpoints(job_id: str, limit: int = 50):
    """查询指定任务的 LangGraph checkpoint 摘要。"""
    service = get_trip_job_service()
    if not service.get_job(job_id):
        raise HTTPException(status_code=404, detail="任务不存在")

    checkpoint_service = get_checkpoint_service()
    checkpoints = checkpoint_service.list_checkpoints(job_id, limit=max(1, min(limit, 200)))
    return TripJobCheckpointsResponse(
        success=True,
        message="Checkpoint查询成功" if checkpoint_service.enabled else "LangGraph Checkpoint未启用",
        job_id=job_id,
        enabled=checkpoint_service.enabled,
        checkpoints=checkpoints,
    )


@router.post(
    "/jobs/{job_id}/replay",
    response_model=TripJobReplayResponse,
    summary="从 LangGraph Checkpoint 恢复/重放旅行规划任务",
    description="基于指定checkpoint创建一个新的异步任务; 不传checkpoint_id时默认从最新checkpoint继续"
)
async def replay_trip_job(
    job_id: str,
    request: TripJobReplayRequest = Body(default_factory=TripJobReplayRequest),
):
    """从指定 checkpoint fork 一个新的异步任务。"""
    service = get_trip_job_service()
    if not service.get_job(job_id):
        raise HTTPException(status_code=404, detail="源任务不存在")

    try:
        checkpoint_id = request.checkpoint_id or get_checkpoint_service().get_latest_checkpoint_id(job_id)
        job = service.create_replay_job(job_id, checkpoint_id)
        return TripJobReplayResponse(
            success=True,
            message="Checkpoint恢复任务已创建",
            source_job_id=job_id,
            checkpoint_id=checkpoint_id,
            job_id=job.job_id,
            status=job.status,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        print(f"[ERROR] 创建Checkpoint恢复任务失败: {exc}")
        raise HTTPException(status_code=500, detail=f"创建Checkpoint恢复任务失败: {exc}")


@router.get(
    "/health",
    summary="健康检查",
    description="检查旅行规划服务是否正常"
)
async def health_check():
    """健康检查"""
    try:
        agent = get_trip_planner_agent()
        
        return {
            "status": "healthy",
            "service": "trip-planner",
            "planner_name": agent.name,
            "workflow": "langgraph"
        }
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"服务不可用: {str(e)}"
        )
