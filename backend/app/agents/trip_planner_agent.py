"""Multi-agent LangGraph travel planning workflow."""

import json
import math
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from ..models.schemas import (
    Attraction,
    Budget,
    DayPlan,
    Hotel,
    Location,
    Meal,
    POIInfo,
    TripPlan,
    TripRequest,
    WeatherInfo,
)
from ..services.amap_service import get_amap_service
from ..services.checkpoint_service import get_checkpoint_service
from ..services.llm_service import get_llm
from ..services.mcp_amap_client import get_amap_mcp_client


MAX_REVISIONS = 1
CRITIC_LLM_MAX_ELAPSED_SECONDS = 90

EventSink = Callable[[str, str, str, Optional[Dict[str, Any]], str], None]
_EVENT_SINKS: Dict[str, EventSink] = {}


class DayRoute(BaseModel):
    """Route grouping for one travel day."""

    day_index: int
    attraction_indices: List[int] = Field(default_factory=list)
    attraction_names: List[str] = Field(default_factory=list)
    estimated_distance_km: float = 0.0
    estimated_transport_minutes: int = 0
    route_reason: str = ""


class RoutePlan(BaseModel):
    """Internal route optimization result."""

    day_routes: List[DayRoute] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class ReviewIssue(BaseModel):
    """One issue found by the critic agent."""

    level: str = "medium"
    message: str
    suggestion: str = ""
    source: str = "rule"


class ReviewReport(BaseModel):
    """Plan review result."""

    passed: bool
    issues: List[ReviewIssue] = Field(default_factory=list)
    needs_revision: bool = False


class TripPlanningState(TypedDict, total=False):
    """Shared state passed between LangGraph agents."""

    request: TripRequest
    attractions: List[Attraction]
    hotels: List[Hotel]
    restaurants: List[Meal]
    weather_info: List[WeatherInfo]
    route_plan: RoutePlan
    plan: TripPlan
    review: ReviewReport
    revision_count: int
    started_at: float
    event_sink: EventSink
    event_sink_id: str
    errors: List[str]


def emit_event(
    state: TripPlanningState,
    agent: str,
    status: str,
    message: str,
    payload: Optional[Dict[str, Any]] = None,
    event_type: str = "progress",
) -> None:
    """Emit a progress event when the workflow is running as an async job."""
    sink = state.get("event_sink")
    if not sink:
        sink_id = state.get("event_sink_id")
        sink = _EVENT_SINKS.get(sink_id) if sink_id else None
    if not sink:
        return

    try:
        sink(agent, status, message, payload or {}, event_type)
    except Exception as exc:
        print(f"[WARN] 发送Agent进度事件失败: {exc}")


PLANNER_SYSTEM_PROMPT = """你是专业旅行规划师。你只负责润色和增强已有行程骨架,不要重写结构化行程。

要求:
1. 只基于输入中的景点、酒店、餐饮、天气和路线摘要写文案,不要新增地点。
2. 保持每日景点顺序,不要改变天数、日期、交通、住宿、预算等结构。
3. 每天 description 控制在 80-140 个中文字符,说明游览重点、节奏和路线理由。
4. overall_suggestions 控制在 120-220 个中文字符,给出预约、天气、交通和节奏建议。
5. 如果 review 中包含修改建议,文案中应体现这些建议。
6. 只返回合法 JSON 对象,不要返回 Markdown。

返回格式:
{
  "day_descriptions": [
    {"day_index": 0, "description": "第1天行程描述"}
  ],
  "overall_suggestions": "总体建议"
}
"""


CRITIC_SYSTEM_PROMPT = """你是旅行计划审核员。你只负责审核,不负责重写计划。

审核重点:
1. 是否符合用户 free_text_input 中的明确要求,例如老人/儿童/忌口/预算/轻松游/必去或不去地点。
2. 行程体验是否明显不合理,例如节奏过赶、连续长距离移动、天气与户外安排冲突。
3. 每日安排是否符合 route_plan 的路线建议。
4. 不要因为文案风格或轻微偏好给 high 级别问题。
5. 如果规则审核已经没有 high 问题,不要因为轻微路线体验、普通景点取舍或表达方式给 high。

问题级别:
- high: 明确违反用户要求,或行程体验明显不可执行,需要重规划。
- medium: 有体验风险,但可以提醒用户或小幅调整。
- low: 轻微优化建议。

只返回合法 JSON 对象,格式:
{
  "issues": [
    {"level": "high|medium|low", "message": "问题", "suggestion": "修改建议"}
  ]
}
"""


class ResearchAgent:
    """Collect real-world travel facts through MCP/AMap tools."""

    def __init__(self, amap_service: Any, amap_mcp: Any):
        self.amap_service = amap_service
        self.amap_mcp = amap_mcp

    def run(self, state: TripPlanningState) -> Dict[str, Any]:
        request = state["request"]
        errors = list(state.get("errors", []))

        print("Agent: ResearchAgent - 搜集景点/天气/酒店/餐饮")
        emit_event(state, "ResearchAgent", "started", "正在搜集景点、天气、酒店和餐饮数据")
        attractions = self._search_attractions(request, errors)
        weather = self._get_weather(request.city, errors)
        hotels = self._search_hotels(request, errors)
        restaurants = self._search_restaurants(request, errors)
        emit_event(
            state,
            "ResearchAgent",
            "completed",
            "真实数据搜集完成",
            {
                "attractions": len(attractions),
                "weather_days": len(weather),
                "hotels": len(hotels),
                "restaurants": len(restaurants),
            },
        )

        return {
            "attractions": attractions,
            "weather_info": weather,
            "hotels": hotels,
            "restaurants": restaurants,
            "errors": errors,
        }

    def _search_poi(self, keywords: str, city: str, citylimit: bool = True, limit: int = 20) -> List[POIInfo]:
        pois = self.amap_mcp.search_poi(keywords, city, citylimit=citylimit, limit=limit)
        if pois or not self.amap_mcp.last_error:
            return pois

        print("[WARN] MCP POI搜索不可用,回退到本地高德服务")
        return self.amap_service.search_poi(keywords, city, citylimit=citylimit, limit=limit)

    def _get_weather(self, city: str, errors: List[str]) -> List[WeatherInfo]:
        weather = self.amap_mcp.get_weather(city)
        if not weather and self.amap_mcp.last_error:
            print("[WARN] MCP天气查询不可用,回退到本地高德服务")
            weather = self.amap_service.get_weather(city)

        if not weather:
            errors.append("未查询到天气预报")
        return weather

    def _search_attractions(self, request: TripRequest, errors: List[str]) -> List[Attraction]:
        keywords = request.preferences or ["景点", "博物馆", "公园"]
        attractions: List[Attraction] = []
        seen: set[str] = set()

        for keyword in keywords[:4]:
            pois = self._search_poi(str(keyword), request.city, limit=12)
            for poi in pois:
                if not poi.name or poi.id in seen or poi.name in seen:
                    continue
                seen.add(poi.id)
                seen.add(poi.name)
                attractions.append(self._poi_to_attraction(poi, str(keyword)))

        if not attractions:
            errors.append("未搜索到景点POI")

        return attractions[: max(6, request.travel_days * 3)]

    def _search_hotels(self, request: TripRequest, errors: List[str]) -> List[Hotel]:
        keyword = request.accommodation if "酒店" in request.accommodation else f"{request.accommodation}酒店"
        pois = self._search_poi(keyword, request.city, limit=10)
        hotels = [self._poi_to_hotel(poi, request.accommodation) for poi in pois[:8]]
        if not hotels:
            errors.append("未搜索到酒店POI")
        return hotels

    def _search_restaurants(self, request: TripRequest, errors: List[str]) -> List[Meal]:
        pois = self._search_poi("特色美食 餐厅", request.city, limit=18)
        restaurants = [self._poi_to_meal(poi, index) for index, poi in enumerate(pois)]
        if not restaurants:
            errors.append("未搜索到餐饮POI")
        return restaurants

    @staticmethod
    def _poi_to_attraction(poi: POIInfo, keyword: str) -> Attraction:
        return Attraction(
            name=poi.name,
            address=poi.address,
            location=poi.location,
            visit_duration=120,
            description=f"{poi.name}位于{poi.address or '当地核心区域'},适合{keyword}主题游览。",
            category=poi.type or keyword or "景点",
            poi_id=poi.id,
            ticket_price=0,
        )

    @staticmethod
    def _poi_to_hotel(poi: POIInfo, accommodation: str) -> Hotel:
        estimated_cost = 350
        if "豪华" in accommodation:
            estimated_cost = 900
        elif "舒适" in accommodation:
            estimated_cost = 550
        elif "民宿" in accommodation:
            estimated_cost = 300

        return Hotel(
            name=poi.name,
            address=poi.address,
            location=poi.location,
            price_range=f"{max(150, estimated_cost - 100)}-{estimated_cost + 150}元",
            rating="",
            distance="建议结合当日景点位置确认通勤距离",
            type=accommodation,
            estimated_cost=estimated_cost,
        )

    @staticmethod
    def _poi_to_meal(poi: POIInfo, index: int) -> Meal:
        meal_types = ["breakfast", "lunch", "dinner"]
        meal_type = meal_types[index % 3]
        cost_map = {"breakfast": 25, "lunch": 60, "dinner": 90}
        return Meal(
            type=meal_type,
            name=poi.name,
            address=poi.address,
            location=poi.location,
            description=f"{poi.type or '当地餐饮'}推荐",
            estimated_cost=cost_map[meal_type],
        )


class RouteAgent:
    """Group and order attractions using coordinates before LLM planning."""

    def run(self, state: TripPlanningState) -> Dict[str, Any]:
        request = state["request"]
        attractions = state.get("attractions") or default_attractions(request)

        print("Agent: RouteAgent - 基于经纬度优化每日路线")
        emit_event(state, "RouteAgent", "started", "正在按经纬度分组并优化每日路线")
        max_per_day = self._max_attractions_per_day(request)
        selected_count = min(len(attractions), max(request.travel_days, request.travel_days * max_per_day))
        selected = attractions[:selected_count]
        ordered_indices = self._nearest_neighbor_order(selected)

        day_routes: List[DayRoute] = []
        cursor = 0
        for day_index in range(request.travel_days):
            remaining_days = request.travel_days - day_index
            remaining_items = len(ordered_indices) - cursor
            if remaining_items <= 0:
                indices = ordered_indices[:1]
            else:
                take = min(max_per_day, max(1, math.ceil(remaining_items / remaining_days)))
                indices = ordered_indices[cursor : cursor + take]
                cursor += take

            names = [selected[i].name for i in indices if i < len(selected)]
            distance_km = self._route_distance_km([selected[i] for i in indices if i < len(selected)])
            transport_minutes = self._estimate_transport_minutes(distance_km, len(indices), request.transportation)
            reason = self._route_reason(names, transport_minutes, request.transportation)
            day_routes.append(
                DayRoute(
                    day_index=day_index,
                    attraction_indices=indices,
                    attraction_names=names,
                    estimated_distance_km=round(distance_km, 2),
                    estimated_transport_minutes=transport_minutes,
                    route_reason=reason,
                )
            )

        warnings = [
            f"第{route.day_index + 1}天预计交通时间较长,建议预留机动时间"
            for route in day_routes
            if route.estimated_transport_minutes > 180
        ]

        emit_event(
            state,
            "RouteAgent",
            "completed",
            "每日路线分组完成",
            {
                "days": len(day_routes),
                "max_attractions_per_day": max_per_day,
                "warnings": warnings,
            },
        )
        return {"route_plan": RoutePlan(day_routes=day_routes, warnings=warnings)}

    @staticmethod
    def _max_attractions_per_day(request: TripRequest) -> int:
        text = request.free_text_input or ""
        if any(word in text for word in ["不累", "轻松", "老人", "小孩", "孩子", "少走路", "无障碍"]):
            return 2
        if any(word in text for word in ["特种兵", "紧凑", "多安排", "尽量多", "打卡"]):
            return 4
        return 3

    @classmethod
    def _nearest_neighbor_order(cls, attractions: List[Attraction]) -> List[int]:
        if not attractions:
            return []

        remaining = set(range(len(attractions)))
        ordered = [0]
        remaining.remove(0)

        while remaining:
            current = ordered[-1]
            next_index = min(
                remaining,
                key=lambda item: cls._distance_km(attractions[current].location, attractions[item].location),
            )
            ordered.append(next_index)
            remaining.remove(next_index)

        return ordered

    @classmethod
    def _route_distance_km(cls, attractions: List[Attraction]) -> float:
        if len(attractions) < 2:
            return 0.0
        return sum(
            cls._distance_km(attractions[i - 1].location, attractions[i].location)
            for i in range(1, len(attractions))
        )

    @staticmethod
    def _distance_km(a: Location, b: Location) -> float:
        radius_km = 6371.0
        lat1 = math.radians(a.latitude)
        lat2 = math.radians(b.latitude)
        delta_lat = math.radians(b.latitude - a.latitude)
        delta_lng = math.radians(b.longitude - a.longitude)

        haversine = (
            math.sin(delta_lat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
        )
        return radius_km * 2 * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine))

    @staticmethod
    def _estimate_transport_minutes(distance_km: float, stop_count: int, transportation: str) -> int:
        if stop_count < 2:
            return 0

        if "步行" in transportation:
            minutes_per_km = 14
        elif "自驾" in transportation:
            minutes_per_km = 5
        else:
            minutes_per_km = 8

        transfer_buffer = max(stop_count - 1, 0) * 8
        return int(round(distance_km * minutes_per_km + transfer_buffer))

    @staticmethod
    def _route_reason(names: List[str], transport_minutes: int, transportation: str) -> str:
        if not names:
            return "未获得可用景点,使用备用行程。"

        joined = "、".join(names)
        if transport_minutes <= 60:
            return f"{joined}距离相对集中,适合使用{transportation}串联游览。"
        return f"{joined}可安排在同一天,但预计交通时间较长,建议放慢节奏。"


class PlannerAgent:
    """Generate a draft TripPlan from researched facts and route guidance."""

    def __init__(self, llm: Any):
        self.llm = llm

    def run(self, state: TripPlanningState) -> Dict[str, Any]:
        print("Agent: PlannerAgent - 生成候选行程")
        emit_event(state, "PlannerAgent", "started", "正在生成候选旅行计划")
        force_rule_based = self._needs_rule_based_revision(state.get("review"))
        base_plan = create_rule_based_plan(state)

        if self.llm.available and state.get("attractions") and not force_rule_based:
            try:
                payload = self._build_llm_payload(state, base_plan)
                payload_text = json.dumps(payload, ensure_ascii=False)
                print(f"PlannerAgent: 调用LLM润色行程文案, payload约{len(payload_text)}字符")
                emit_event(
                    state,
                    "PlannerAgent",
                    "llm_started",
                    "正在调用LLM润色行程文案",
                    {"payload_chars": len(payload_text)},
                )
                data = self._call_llm_polish(payload_text)
                polished_plan = self._apply_llm_polish(base_plan, data)
                print("PlannerAgent: LLM行程文案润色完成")
                emit_event(state, "PlannerAgent", "llm_completed", "LLM行程文案润色完成")
                return {"plan": polished_plan}
            except Exception as e:
                print(f"[WARN] LLM行程文案润色失败,使用规则化规划: {str(e)}")
                emit_event(
                    state,
                    "PlannerAgent",
                    "llm_failed",
                    "LLM行程文案润色失败,将使用规则化方案",
                    {"error": str(e)},
                )
                errors = list(state.get("errors", []))
                errors.append(f"LLM行程文案润色失败: {str(e)}")
                state = {**state, "errors": errors}

        if force_rule_based:
            print("PlannerAgent: 重规划阶段使用规则化保守方案,避免重复等待LLM")
            emit_event(state, "PlannerAgent", "rule_based", "重规划阶段使用规则化保守方案")
        else:
            emit_event(state, "PlannerAgent", "rule_based", "使用规则化方案生成行程")
        return {"plan": base_plan, "errors": state.get("errors", [])}

    @staticmethod
    def _needs_rule_based_revision(review: Optional[ReviewReport]) -> bool:
        return bool(review and review.needs_revision)

    def _call_llm_polish(self, payload_text: str) -> Dict[str, Any]:
        messages = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "请根据以下 JSON 数据润色旅行计划文案。"
                    "只返回 JSON 对象,不要返回 Markdown。\n\n"
                    f"输入数据:\n{payload_text}"
                ),
            },
        ]

        last_error: Optional[Exception] = None
        for attempt in range(2):
            try:
                return self.llm.chat_json(messages, temperature=0.2)
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    print(f"[WARN] PlannerAgent LLM润色请求失败,准备重试: {exc}")

        raise last_error or RuntimeError("LLM润色请求失败")

    @staticmethod
    def _build_llm_payload(state: TripPlanningState, base_plan: TripPlan) -> Dict[str, Any]:
        request = state["request"]
        route_plan = state.get("route_plan")
        review = state.get("review")
        return {
            "request": {
                "city": request.city,
                "start_date": request.start_date,
                "end_date": request.end_date,
                "travel_days": request.travel_days,
                "transportation": request.transportation,
                "accommodation": request.accommodation,
                "preferences": request.preferences,
                "free_text_input": request.free_text_input,
            },
            "days": [
                {
                    "day_index": day.day_index,
                    "date": day.date,
                    "current_description": day.description,
                    "attractions": [item.name for item in day.attractions],
                    "meals": [item.name for item in day.meals],
                    "hotel": day.hotel.name if day.hotel else "",
                    "weather": weather_summary_for_day(base_plan.weather_info, day.day_index),
                    "route_reason": route_note_for_day(day.day_index, route_plan),
                }
                for day in base_plan.days
            ],
            "route_warnings": route_plan.warnings if route_plan else [],
            "review": review.model_dump() if review else None,
            "notes": state.get("errors", []),
        }

    @staticmethod
    def _apply_llm_polish(plan: TripPlan, data: Dict[str, Any]) -> TripPlan:
        day_descriptions = data.get("day_descriptions", [])
        if isinstance(day_descriptions, list):
            for item in day_descriptions:
                if not isinstance(item, dict):
                    continue
                try:
                    day_index = int(item.get("day_index", -1))
                except (TypeError, ValueError):
                    continue
                description = str(item.get("description") or "").strip()
                if description and 0 <= day_index < len(plan.days):
                    plan.days[day_index].description = description

        overall_suggestions = str(data.get("overall_suggestions") or "").strip()
        if overall_suggestions:
            plan.overall_suggestions = overall_suggestions
        return plan


class CriticAgent:
    """Review, normalize, and request revision for rule or LLM critic issues."""

    def __init__(self, llm: Any):
        self.llm = llm

    def run(self, state: TripPlanningState) -> Dict[str, Any]:
        print("Agent: CriticAgent - 审核行程并决定是否重规划")
        emit_event(state, "CriticAgent", "started", "正在审核行程结构、体验和约束")
        request = state["request"]
        errors = list(state.get("errors", []))
        issues: List[ReviewIssue] = []
        plan = state.get("plan")

        if not isinstance(plan, TripPlan):
            issues.append(
                ReviewIssue(
                    level="high",
                    message="规划结果不是TripPlan对象",
                    suggestion="使用规则化规划重新生成完整TripPlan。",
                )
            )
            plan = create_rule_based_plan(state)

        if len(plan.days) != request.travel_days:
            issues.append(
                ReviewIssue(
                    level="high",
                    message="规划天数与用户请求不一致",
                    suggestion="按 travel_days 重新生成每日行程。",
                )
            )
            plan = create_rule_based_plan(state)

        plan = normalize_plan(plan, request)
        rule_issues = self._review_plan_content(plan, state)
        issues.extend(rule_issues)
        issues.extend(self._review_plan_with_llm(plan, state, rule_issues, errors))
        issues = self._normalize_issue_levels(issues, state)
        self._log_issues(issues)

        has_high_issue = any(issue.level == "high" for issue in issues)
        should_revise = self._should_request_revision(issues, state)
        revision_count = state.get("revision_count", 0)
        needs_revision = should_revise and revision_count < MAX_REVISIONS
        passed = not has_high_issue

        if needs_revision:
            revision_count += 1
            print("[WARN] CriticAgent发现高优先级问题,触发一次重规划")
            emit_event(
                state,
                "CriticAgent",
                "revision_requested",
                "审核发现高优先级问题,触发一次重规划",
                {"issues": [issue.model_dump() for issue in issues]},
            )
        elif should_revise:
            errors.append("CriticAgent发现问题但已达到重规划次数上限,返回已规范化的保守方案")

        review = ReviewReport(passed=passed, issues=issues, needs_revision=needs_revision)
        emit_event(
            state,
            "CriticAgent",
            "completed",
            "行程审核完成",
            {
                "passed": passed,
                "needs_revision": needs_revision,
                "issues": [issue.model_dump() for issue in issues],
            },
        )
        return {
            "plan": plan,
            "review": review,
            "revision_count": revision_count,
            "errors": errors,
        }

    def _review_plan_content(self, plan: TripPlan, state: TripPlanningState) -> List[ReviewIssue]:
        request = state["request"]
        route_plan = state.get("route_plan")
        trusted_attractions = {item.name for item in state.get("attractions", [])}
        max_per_day = RouteAgent._max_attractions_per_day(request)
        issues: List[ReviewIssue] = []

        for day in plan.days:
            if not day.attractions:
                issues.append(
                    ReviewIssue(
                        level="high",
                        message=f"第{day.day_index + 1}天没有安排景点",
                        suggestion="至少安排一个真实景点或使用备用景点。",
                    )
                )

            if len(day.attractions) > max_per_day:
                issues.append(
                    ReviewIssue(
                        level="high",
                        message=f"第{day.day_index + 1}天景点数量超过当前节奏限制",
                        suggestion=f"每天最多安排{max_per_day}个景点。",
                    )
                )

            if len(day.meals) < 3:
                issues.append(
                    ReviewIssue(
                        level="high",
                        message=f"第{day.day_index + 1}天餐饮安排不足三餐",
                        suggestion="补齐早餐、午餐和晚餐。",
                    )
                )

            if trusted_attractions:
                for attraction in day.attractions:
                    if attraction.name not in trusted_attractions:
                        issues.append(
                            ReviewIssue(
                                level="high",
                                message=f"景点“{attraction.name}”不在真实POI候选列表中",
                                suggestion="只使用ResearchAgent返回的真实景点。",
                            )
                        )

        if route_plan:
            for route in route_plan.day_routes:
                if route.estimated_transport_minutes > 240:
                    issues.append(
                        ReviewIssue(
                            level="medium",
                            message=f"第{route.day_index + 1}天预计交通时间较长",
                            suggestion="建议减少跨区景点或预留更多休息时间。",
                        )
                    )

        expected_budget = calculate_budget(plan)
        if plan.budget and plan.budget.total != expected_budget.total:
            issues.append(
                ReviewIssue(
                    level="medium",
                    message="预算总额与分项估算不一致",
                    suggestion="已按分项重新计算预算。",
                )
            )
            plan.budget = expected_budget

        return issues

    def _normalize_issue_levels(
        self,
        issues: List[ReviewIssue],
        state: TripPlanningState,
    ) -> List[ReviewIssue]:
        normalized: List[ReviewIssue] = []
        has_rule_high = any(issue.level == "high" and issue.source == "rule" for issue in issues)

        for issue in issues:
            if issue.source == "llm" and issue.level == "high" and not has_rule_high:
                if not self._is_llm_high_issue_actionable(issue, state):
                    normalized.append(
                        issue.model_copy(
                            update={
                                "level": "medium",
                                "suggestion": (
                                    issue.suggestion
                                    or "作为体验优化建议保留,不触发自动重规划。"
                                ),
                            }
                        )
                    )
                    continue
            normalized.append(issue)

        return normalized

    @staticmethod
    def _should_request_revision(issues: List[ReviewIssue], state: TripPlanningState) -> bool:
        if any(issue.level == "high" and issue.source == "rule" for issue in issues):
            return True

        return any(
            issue.level == "high"
            and issue.source == "llm"
            and CriticAgent._is_llm_high_issue_actionable(issue, state)
            for issue in issues
        )

    @staticmethod
    def _is_llm_high_issue_actionable(issue: ReviewIssue, state: TripPlanningState) -> bool:
        text = f"{issue.message} {issue.suggestion}"
        request_text = state["request"].free_text_input or ""

        hard_keywords = [
            "明确要求",
            "违反用户",
            "必去",
            "不要",
            "不去",
            "过敏",
            "忌口",
            "老人",
            "小孩",
            "儿童",
            "无障碍",
            "不可执行",
            "无法完成",
            "开放时间",
            "闭馆",
            "关闭",
            "天气",
            "暴雨",
            "台风",
            "危险",
        ]
        if any(keyword in text for keyword in hard_keywords):
            return True

        # If the user wrote explicit free-text constraints, let LLM high issues
        # related to those constraints trigger one revision.
        return bool(request_text and any(token in text for token in request_text.split() if len(token) >= 2))

    @staticmethod
    def _log_issues(issues: List[ReviewIssue]) -> None:
        if not issues:
            print("CriticAgent: 未发现审核问题")
            return

        print("CriticAgent: 审核问题明细:")
        for index, issue in enumerate(issues, start=1):
            suggestion = f" | 建议: {issue.suggestion}" if issue.suggestion else ""
            print(
                f"  {index}. [{issue.level.upper()}][{issue.source}] "
                f"{issue.message}{suggestion}"
            )

    def _review_plan_with_llm(
        self,
        plan: TripPlan,
        state: TripPlanningState,
        rule_issues: List[ReviewIssue],
        errors: List[str],
    ) -> List[ReviewIssue]:
        if not self.llm.available:
            return []
        if state.get("revision_count", 0) > 0:
            print("CriticAgent: 重规划后跳过LLM复审,使用规则审核结果")
            emit_event(state, "CriticAgent", "llm_skipped", "重规划后跳过LLM复审,使用规则审核结果")
            return []
        started_at = state.get("started_at")
        if started_at and time.monotonic() - started_at > CRITIC_LLM_MAX_ELAPSED_SECONDS:
            print("CriticAgent: 当前请求耗时较长,跳过LLM审核以避免前端超时")
            emit_event(state, "CriticAgent", "llm_skipped", "当前请求耗时较长,跳过LLM审核以避免前端超时")
            return []

        try:
            route_plan = state.get("route_plan")
            payload = {
                "request": state["request"].model_dump(),
                "plan_summary": self._build_plan_review_summary(plan, route_plan),
                "rule_issues": [issue.model_dump() for issue in rule_issues],
                "available_attractions": [item.name for item in state.get("attractions", [])],
            }
            payload_text = json.dumps(payload, ensure_ascii=False)
            print(f"CriticAgent: 调用LLM审核行程体验, payload约{len(payload_text)}字符")
            emit_event(
                state,
                "CriticAgent",
                "llm_started",
                "正在调用LLM审核行程体验",
                {"payload_chars": len(payload_text)},
            )
            data = self.llm.chat_json(
                [
                    {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            "请审核以下旅行计划是否符合用户需求和路线体验。"
                            "只返回 JSON,不要返回 Markdown。\n\n"
                            f"{payload_text}"
                        ),
                    },
                ],
                temperature=0.1,
            )
            print("CriticAgent: LLM审核完成")
            emit_event(state, "CriticAgent", "llm_completed", "LLM行程体验审核完成")
        except Exception as e:
            print(f"[WARN] LLM审核失败,继续使用规则审核: {str(e)}")
            emit_event(
                state,
                "CriticAgent",
                "llm_failed",
                "LLM审核失败,继续使用规则审核",
                {"error": str(e)},
            )
            errors.append(f"LLM审核失败: {str(e)}")
            return []

        issues: List[ReviewIssue] = []
        raw_issues = data.get("issues", [])
        if not isinstance(raw_issues, list):
            return []

        allowed_levels = {"high", "medium", "low"}
        for item in raw_issues[:6]:
            if not isinstance(item, dict):
                continue
            message = str(item.get("message") or "").strip()
            if not message:
                continue
            level = str(item.get("level") or "medium").strip().lower()
            if level not in allowed_levels:
                level = "medium"
            issues.append(
                ReviewIssue(
                    level=level,
                    message=message,
                    suggestion=str(item.get("suggestion") or "").strip(),
                    source="llm",
                )
            )

        return issues

    @staticmethod
    def _build_plan_review_summary(plan: TripPlan, route_plan: Optional[RoutePlan]) -> Dict[str, Any]:
        return {
            "city": plan.city,
            "start_date": plan.start_date,
            "end_date": plan.end_date,
            "days": [
                {
                    "day_index": day.day_index,
                    "date": day.date,
                    "description": day.description,
                    "attractions": [item.name for item in day.attractions],
                    "meal_count": len(day.meals),
                    "hotel": day.hotel.name if day.hotel else "",
                    "route": route_note_for_day(day.day_index, route_plan),
                }
                for day in plan.days
            ],
            "overall_suggestions": plan.overall_suggestions,
            "budget_total": plan.budget.total if plan.budget else 0,
            "route_warnings": route_plan.warnings if route_plan else [],
        }


class LangGraphTripPlanner:
    """Multi-agent travel planner orchestrated by LangGraph."""

    def __init__(self):
        print("初始化 LangGraph 多Agent旅行规划工作流...")
        self.amap_service = get_amap_service()
        self.amap_mcp = get_amap_mcp_client()
        self.llm = get_llm()
        self.research_agent = ResearchAgent(self.amap_service, self.amap_mcp)
        self.route_agent = RouteAgent()
        self.planner_agent = PlannerAgent(self.llm)
        self.critic_agent = CriticAgent(self.llm)
        self.checkpoint_service = get_checkpoint_service()
        self.graph = self._build_graph()
        print("[OK] LangGraph 多Agent旅行规划工作流初始化成功")

    @property
    def name(self) -> str:
        return "LangGraph多Agent旅行规划工作流"

    def plan_trip(
        self,
        request: TripRequest,
        event_sink: Optional[EventSink] = None,
        checkpoint_thread_id: Optional[str] = None,
    ) -> TripPlan:
        """Generate a travel plan from the user's request."""
        thread_id = checkpoint_thread_id or uuid.uuid4().hex
        initial_state: TripPlanningState = {
            "request": request,
            "errors": [],
            "revision_count": 0,
            "started_at": time.monotonic(),
        }
        if event_sink:
            _EVENT_SINKS[thread_id] = event_sink
            initial_state["event_sink_id"] = thread_id

        try:
            print(f"\n{'=' * 60}")
            print("开始 LangGraph 多Agent旅行规划")
            print(f"目的地: {request.city}")
            print(f"日期: {request.start_date} 至 {request.end_date}")
            print(f"偏好: {', '.join(request.preferences) if request.preferences else '无'}")
            print(f"{'=' * 60}\n")
            emit_event(initial_state, "LangGraphTripPlanner", "started", "多Agent旅行规划工作流开始")

            config = {"configurable": {"thread_id": thread_id}}
            result = self.graph.invoke(initial_state, config)
            plan = result.get("plan")
            if isinstance(plan, TripPlan):
                final_plan = normalize_plan(plan, request)
                emit_event(
                    initial_state,
                    "LangGraphTripPlanner",
                    "completed",
                    "多Agent旅行规划工作流完成",
                    {"city": final_plan.city, "days": len(final_plan.days)},
                )
                return final_plan

            fallback_plan = create_fallback_plan(request, result)
            emit_event(
                initial_state,
                "LangGraphTripPlanner",
                "fallback",
                "工作流未返回有效计划,已使用备用方案",
            )
            return fallback_plan

        except Exception as e:
            print(f"[ERROR] LangGraph多Agent旅行规划失败: {str(e)}")
            emit_event(
                initial_state,
                "LangGraphTripPlanner",
                "failed",
                "多Agent旅行规划失败,已使用备用方案",
                {"error": str(e)},
            )
            return create_fallback_plan(request, {"errors": [str(e)]})
        finally:
            if event_sink:
                _EVENT_SINKS.pop(thread_id, None)

    def replay_trip_from_checkpoint(
        self,
        request: TripRequest,
        checkpoint_thread_id: str,
        checkpoint_id: str,
        event_sink: Optional[EventSink] = None,
    ) -> TripPlan:
        """Continue the workflow from a persisted checkpoint."""
        runtime_state: TripPlanningState = {
            "request": request,
            "started_at": time.monotonic(),
        }
        if event_sink:
            _EVENT_SINKS[checkpoint_thread_id] = event_sink
            runtime_state["event_sink_id"] = checkpoint_thread_id

        try:
            print(f"\n{'=' * 60}")
            print("开始 LangGraph checkpoint replay/resume")
            print(f"Thread ID: {checkpoint_thread_id}")
            print(f"Checkpoint ID: {checkpoint_id}")
            print(f"目的地: {request.city}")
            print(f"{'=' * 60}\n")
            emit_event(runtime_state, "LangGraphTripPlanner", "replay_started", "正在从Checkpoint恢复工作流")

            config = self.checkpoint_service.get_checkpoint_config(checkpoint_thread_id, checkpoint_id)
            replay_config = self.graph.update_state(
                config,
                {
                    "event_sink_id": checkpoint_thread_id if event_sink else "",
                    "started_at": time.monotonic(),
                },
            )
            result = self.graph.invoke(None, replay_config)
            plan = result.get("plan")
            if isinstance(plan, TripPlan):
                final_plan = normalize_plan(plan, request)
                emit_event(
                    runtime_state,
                    "LangGraphTripPlanner",
                    "replay_completed",
                    "Checkpoint恢复执行完成",
                    {"city": final_plan.city, "days": len(final_plan.days)},
                )
                return final_plan

            fallback_plan = create_fallback_plan(request, result)
            emit_event(
                runtime_state,
                "LangGraphTripPlanner",
                "replay_fallback",
                "Checkpoint恢复未返回有效计划,已使用备用方案",
            )
            return fallback_plan
        except Exception as e:
            print(f"[ERROR] LangGraph checkpoint replay/resume失败: {str(e)}")
            emit_event(
                runtime_state,
                "LangGraphTripPlanner",
                "replay_failed",
                "Checkpoint恢复执行失败,已使用备用方案",
                {"error": str(e)},
            )
            return create_fallback_plan(request, {"errors": [str(e)]})
        finally:
            if event_sink:
                _EVENT_SINKS.pop(checkpoint_thread_id, None)

    def _build_graph(self):
        workflow = StateGraph(TripPlanningState)
        workflow.add_node("research", self.research_agent.run)
        workflow.add_node("route", self.route_agent.run)
        workflow.add_node("plan", self.planner_agent.run)
        workflow.add_node("critique", self.critic_agent.run)

        workflow.add_edge(START, "research")
        workflow.add_edge("research", "route")
        workflow.add_edge("route", "plan")
        workflow.add_edge("plan", "critique")
        workflow.add_conditional_edges(
            "critique",
            self._should_revise,
            {
                "revise": "plan",
                "final": END,
            },
        )

        return workflow.compile(checkpointer=self.checkpoint_service.checkpointer)

    @staticmethod
    def _should_revise(state: TripPlanningState) -> str:
        review = state.get("review")
        if review and review.needs_revision:
            return "revise"
        return "final"


def create_rule_based_plan(state: TripPlanningState) -> TripPlan:
    request = state["request"]
    start_date = datetime.strptime(request.start_date, "%Y-%m-%d")
    attractions = state.get("attractions") or default_attractions(request)
    hotels = state.get("hotels") or default_hotels(request)
    restaurants = state.get("restaurants") or default_restaurants(request)
    weather = weather_for_trip_days(state)
    route_plan = state.get("route_plan")

    days: List[DayPlan] = []
    attraction_cursor = 0
    meal_cursor = 0

    for day_index in range(request.travel_days):
        current_date = start_date + timedelta(days=day_index)
        day_attractions = attractions_for_day(day_index, attractions, route_plan)
        if not day_attractions:
            day_attractions = attractions[attraction_cursor : attraction_cursor + 3]
            attraction_cursor += len(day_attractions)
        if not day_attractions:
            day_attractions = attractions[: min(3, len(attractions))]

        meals = pick_daily_meals(restaurants, meal_cursor)
        meal_cursor += 3
        hotel = hotels[day_index % len(hotels)] if hotels else None
        route_note = route_note_for_day(day_index, route_plan)

        days.append(
            DayPlan(
                date=current_date.strftime("%Y-%m-%d"),
                day_index=day_index,
                description=build_day_description(day_index, day_attractions, weather, route_note),
                transportation=request.transportation,
                accommodation=request.accommodation,
                hotel=hotel,
                attractions=day_attractions,
                meals=meals,
            )
        )

    plan = TripPlan(
        city=request.city,
        start_date=request.start_date,
        end_date=request.end_date,
        days=days,
        weather_info=weather,
        overall_suggestions=build_overall_suggestions(request, state),
        budget=None,
    )
    plan.budget = calculate_budget(plan)
    return plan


def create_fallback_plan(request: TripRequest, state: Optional[Dict[str, Any]] = None) -> TripPlan:
    fallback_state: TripPlanningState = {
        "request": request,
        "errors": list((state or {}).get("errors", [])),
    }
    return create_rule_based_plan(fallback_state)


def normalize_plan(plan: TripPlan, request: TripRequest) -> TripPlan:
    plan.city = request.city
    plan.start_date = request.start_date
    plan.end_date = request.end_date

    start_date = datetime.strptime(request.start_date, "%Y-%m-%d")
    for index, day in enumerate(plan.days):
        day.day_index = index
        day.date = (start_date + timedelta(days=index)).strftime("%Y-%m-%d")
        day.transportation = day.transportation or request.transportation
        day.accommodation = day.accommodation or request.accommodation
        if len(day.meals) < 3:
            day.meals = pick_daily_meals(day.meals, 0)

    if len(plan.weather_info) < request.travel_days:
        state: TripPlanningState = {"request": request, "weather_info": plan.weather_info}
        plan.weather_info = weather_for_trip_days(state)

    plan.budget = calculate_budget(plan)
    return plan


def weather_for_trip_days(state: TripPlanningState) -> List[WeatherInfo]:
    request = state["request"]
    weather = list(state.get("weather_info") or [])
    start_date = datetime.strptime(request.start_date, "%Y-%m-%d")

    if not weather:
        return [
            WeatherInfo(
                date=(start_date + timedelta(days=i)).strftime("%Y-%m-%d"),
                day_weather="未知",
                night_weather="未知",
                day_temp=0,
                night_temp=0,
                wind_direction="",
                wind_power="",
            )
            for i in range(request.travel_days)
        ]

    normalized: List[WeatherInfo] = []
    for i in range(request.travel_days):
        item = weather[min(i, len(weather) - 1)].model_copy(deep=True)
        item.date = (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
        normalized.append(item)
    return normalized


def attractions_for_day(day_index: int, attractions: List[Attraction], route_plan: Optional[RoutePlan]) -> List[Attraction]:
    if not route_plan:
        return []

    for route in route_plan.day_routes:
        if route.day_index == day_index:
            return [
                attractions[index].model_copy(deep=True)
                for index in route.attraction_indices
                if 0 <= index < len(attractions)
            ]
    return []


def route_note_for_day(day_index: int, route_plan: Optional[RoutePlan]) -> str:
    if not route_plan:
        return ""

    for route in route_plan.day_routes:
        if route.day_index == day_index:
            return route.route_reason
    return ""


def weather_summary_for_day(weather: List[WeatherInfo], day_index: int) -> str:
    if not weather or day_index >= len(weather):
        return ""

    item = weather[day_index]
    weather_text = item.day_weather or item.night_weather or ""
    if not weather_text:
        return ""
    return f"{weather_text}, {item.night_temp}-{item.day_temp}度"


def pick_daily_meals(restaurants: List[Meal], cursor: int) -> List[Meal]:
    if not restaurants:
        return default_daily_meals()

    selected = [restaurants[(cursor + i) % len(restaurants)].model_copy(deep=True) for i in range(3)]
    for meal, meal_type in zip(selected, ["breakfast", "lunch", "dinner"]):
        meal.type = meal_type
    return selected


def default_daily_meals() -> List[Meal]:
    return [
        Meal(type="breakfast", name="当地特色早餐", description="选择酒店附近早餐店", estimated_cost=25),
        Meal(type="lunch", name="景区附近午餐", description="结合上午景点就近用餐", estimated_cost=60),
        Meal(type="dinner", name="城市特色晚餐", description="体验当地代表性餐饮", estimated_cost=90),
    ]


def build_day_description(
    day_index: int,
    attractions: List[Attraction],
    weather: List[WeatherInfo],
    route_note: str = "",
) -> str:
    names = "、".join(item.name for item in attractions) or "城市核心景点"
    weather_text = ""
    if day_index < len(weather) and weather[day_index].day_weather:
        weather_text = f", 当天天气参考: {weather[day_index].day_weather}"
    route_text = f" {route_note}" if route_note else ""
    return f"第{day_index + 1}天安排{names}{weather_text}。{route_text}"


def build_overall_suggestions(request: TripRequest, state: TripPlanningState) -> str:
    suggestions = [
        f"这是为您规划的{request.city}{request.travel_days}日游行程。",
        "建议出行前再次确认景点开放时间、门票预约和天气变化。",
    ]
    route_plan = state.get("route_plan")
    if route_plan and route_plan.warnings:
        suggestions.append("路线提示:" + "；".join(route_plan.warnings))
    if request.free_text_input:
        suggestions.append(f"已考虑额外要求: {request.free_text_input}")
    if state.get("errors"):
        suggestions.append("部分实时数据不可用时已使用保守备用方案。")
    return "".join(suggestions)


def calculate_budget(plan: TripPlan) -> Budget:
    total_attractions = 0
    total_hotels = 0
    total_meals = 0

    for day in plan.days:
        total_attractions += sum(item.ticket_price or 0 for item in day.attractions)
        if day.hotel:
            total_hotels += day.hotel.estimated_cost or 0
        total_meals += sum(item.estimated_cost or 0 for item in day.meals)

    total_transportation = max(30 * len(plan.days), 0)
    total = total_attractions + total_hotels + total_meals + total_transportation

    return Budget(
        total_attractions=total_attractions,
        total_hotels=total_hotels,
        total_meals=total_meals,
        total_transportation=total_transportation,
        total=total,
    )


def default_attractions(request: TripRequest) -> List[Attraction]:
    base_lng = 116.397128
    base_lat = 39.916527
    return [
        Attraction(
            name=f"{request.city}代表景点{i + 1}",
            address=f"{request.city}市",
            location=Location(longitude=base_lng + i * 0.01, latitude=base_lat + i * 0.01),
            visit_duration=120,
            description="实时景点数据不可用时生成的占位景点,请以实际查询为准。",
            category="景点",
            ticket_price=0,
        )
        for i in range(max(3, request.travel_days * 2))
    ]


def default_hotels(request: TripRequest) -> List[Hotel]:
    return [
        Hotel(
            name=f"{request.city}{request.accommodation}推荐",
            address=f"{request.city}市中心区域",
            location=None,
            price_range="300-500元",
            rating="",
            distance="建议选择靠近主要景点或地铁站的位置",
            type=request.accommodation,
            estimated_cost=350,
        )
    ]


def default_restaurants(request: TripRequest) -> List[Meal]:
    return [
        Meal(type="breakfast", name=f"{request.city}特色早餐", description="当地早餐", estimated_cost=25),
        Meal(type="lunch", name=f"{request.city}特色午餐", description="当地午餐", estimated_cost=60),
        Meal(type="dinner", name=f"{request.city}特色晚餐", description="当地晚餐", estimated_cost=90),
    ]


# Backward-compatible alias used by the existing FastAPI route.
MultiAgentTripPlanner = LangGraphTripPlanner

_trip_planner: Optional[LangGraphTripPlanner] = None


def get_trip_planner_agent() -> LangGraphTripPlanner:
    """获取 LangGraph 多Agent旅行规划工作流实例(单例模式)."""
    global _trip_planner

    if _trip_planner is None:
        _trip_planner = LangGraphTripPlanner()

    return _trip_planner
