# Agent 模块端到端讲解

本文档不是只讲 `trip_planner_agent.py`，而是把 Agent 相关的前后端文件串起来讲清楚。

你现在看不懂的核心原因通常不是某一行 Python 难，而是上下文断了：

- `TripRequest` 是哪里来的?
- 为什么它要这样设计?
- 前端表单和后端模型怎么对应?
- 为什么初始 State 只有 `request` 和 `errors`?
- 高德搜索结果怎么变成景点?
- LLM 在什么时候介入?
- 图片为什么又不在 Agent 主流程里?

这份文档按一次真实请求的生命周期来讲。

## 0. 先记住一条主线

用户点击“开始规划我的旅行”后，整个系统走这条线：

```text
frontend/src/views/Home.vue
  收集用户表单

frontend/src/services/api.ts
  POST /api/trip/plan

backend/app/api/routes/trip.py
  FastAPI 接收 JSON,解析成 TripRequest

backend/app/models/schemas.py
  TripRequest 定义请求长什么样

backend/app/agents/trip_planner_agent.py
  LangGraph Agent 工作流

backend/app/services/amap_service.py
  调高德: 景点、天气、酒店、餐饮

backend/app/services/llm_service.py
  调 Qwen / OpenAI-compatible LLM

backend/app/models/schemas.py
  TripPlan 定义最终返回结构

frontend/src/views/Result.vue
  展示行程、地图、图片、预算
```

所以学习 Agent 不能只看一个文件。`trip_planner_agent.py` 是核心大脑，但它的输入、输出、工具和展示都来自其他文件。

## 1. 用户输入从哪里来: Home.vue

文件:

```text
frontend/src/views/Home.vue
```

页面上用户填写:

- 目的地城市
- 开始日期
- 结束日期
- 旅行天数
- 交通方式
- 住宿偏好
- 旅行偏好
- 额外要求

这些字段在前端状态里是:

```ts
const formData = reactive<TripFormState>({
  city: '',
  start_date: null,
  end_date: null,
  travel_days: 1,
  transportation: '公共交通',
  accommodation: '经济型酒店',
  preferences: [],
  free_text_input: ''
})
```

这里的 `TripFormState` 是前端表单状态。注意它和后端的 `TripRequest` 很像，但不完全一样。

为什么不完全一样?

因为前端日期选择器拿到的是 `Dayjs` 对象:

```ts
start_date: Dayjs | null
end_date: Dayjs | null
```

但后端不能接收 `Dayjs` 对象。后端只适合接收 JSON，而 JSON 里日期一般用字符串:

```json
"2026-06-25"
```

所以提交时会转换:

```ts
const requestData: TripFormData = {
  city: formData.city,
  start_date: formData.start_date.format('YYYY-MM-DD'),
  end_date: formData.end_date.format('YYYY-MM-DD'),
  travel_days: formData.travel_days,
  transportation: formData.transportation,
  accommodation: formData.accommodation,
  preferences: formData.preferences,
  free_text_input: formData.free_text_input
}
```

这一步很关键。

它把“前端表单状态”变成了“后端请求数据”。

## 2. 前端请求类型: TripFormData

文件:

```text
frontend/src/types/index.ts
```

前端定义:

```ts
export interface TripFormData {
  city: string
  start_date: string
  end_date: string
  travel_days: number
  transportation: string
  accommodation: string
  preferences: string[]
  free_text_input: string
}
```

这就是前端准备发给后端的数据结构。

它和后端 `TripRequest` 基本对应:

```text
前端 TripFormData  ->  后端 TripRequest
```

为什么要前后端都定义一份?

因为它们运行在两个世界:

- 前端是 TypeScript，需要 `interface` 帮助检查代码。
- 后端是 Python，需要 Pydantic 模型校验请求。

两边字段保持一致，系统才能稳定通信。

## 3. 请求是怎么发出去的: api.ts

文件:

```text
frontend/src/services/api.ts
```

核心代码:

```ts
export async function generateTripPlan(formData: TripFormData): Promise<TripPlanResponse> {
  const response = await apiClient.post<TripPlanResponse>('/api/trip/plan', formData)
  return response.data
}
```

意思是:

```text
把 TripFormData 作为 JSON body 发给后端 /api/trip/plan
```

因为 `apiClient` 的 baseURL 是:

```ts
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000'
```

所以真实请求地址是:

```text
http://localhost:8000/api/trip/plan
```

一个请求例子:

```json
{
  "city": "马鞍山",
  "start_date": "2026-06-25",
  "end_date": "2026-06-26",
  "travel_days": 2,
  "transportation": "混合",
  "accommodation": "经济型酒店",
  "preferences": ["自然风光"],
  "free_text_input": "不要太累"
}
```

到这里为止，还没有 Agent。这里只是在准备 Agent 的输入。

## 4. 后端如何接住请求: routes/trip.py

文件:

```text
backend/app/api/routes/trip.py
```

核心代码:

```python
@router.post("/plan", response_model=TripPlanResponse)
async def plan_trip(request: TripRequest):
```

这里有两个非常重要的类型。

### 4.1 `request: TripRequest`

这表示:

```text
FastAPI 会把前端传来的 JSON 自动解析成 TripRequest 对象。
```

也就是说，前端发来:

```json
{
  "city": "马鞍山",
  "start_date": "2026-06-25",
  ...
}
```

FastAPI 会自动变成 Python 对象:

```python
TripRequest(
    city="马鞍山",
    start_date="2026-06-25",
    ...
)
```

所以你看到 Agent 里有:

```python
request.city
request.travel_days
request.preferences
```

这些不是凭空来的，是 FastAPI 根据 `TripRequest` 自动创建的。

### 4.2 `response_model=TripPlanResponse`

这表示接口最终应该返回:

```python
TripPlanResponse
```

也就是这种结构:

```json
{
  "success": true,
  "message": "旅行计划生成成功",
  "data": {
    "city": "...",
    "days": [...],
    "weather_info": [...],
    "budget": {...}
  }
}
```

路由函数里真正调用 Agent 的地方是:

```python
agent = get_trip_planner_agent()
trip_plan = agent.plan_trip(request)
```

所以:

```text
TripRequest 是 Agent 的输入
TripPlan 是 Agent 的输出
TripPlanResponse 是 API 包装后的响应
```

## 5. TripRequest 为什么这样设计

文件:

```text
backend/app/models/schemas.py
```

定义:

```python
class TripRequest(BaseModel):
    city: str
    start_date: str
    end_date: str
    travel_days: int
    transportation: str
    accommodation: str
    preferences: List[str]
    free_text_input: Optional[str]
```

你问得很好: 为什么 `TripRequest` 要这样设计?

答案是: 它不是随便列字段，而是在描述“生成旅行计划所需的最小用户意图”。

### 5.1 `city`

```python
city: str
```

这是所有工具调用的核心参数。

高德搜索景点需要城市:

```python
self.amap_service.search_poi(keyword, request.city)
```

天气查询需要城市:

```python
self.amap_service.get_weather(request.city)
```

酒店和餐厅搜索也需要城市。

没有 `city`，后面所有节点都不知道去哪查。

### 5.2 `start_date` 和 `end_date`

```python
start_date: str
end_date: str
```

它们定义旅行日期范围。

Agent 用它生成每天的日期:

```python
start_date = datetime.strptime(request.start_date, "%Y-%m-%d")
current_date = start_date + timedelta(days=day_index)
```

为什么是字符串而不是 Python 的 `date`?

因为前端传 JSON 时日期天然是字符串，写成 `YYYY-MM-DD` 最直观，也方便展示。

### 5.3 `travel_days`

```python
travel_days: int = Field(..., ge=1, le=30)
```

这是旅行天数。

它影响很多地方:

搜索景点数量:

```python
attractions[: max(6, request.travel_days * 3)]
```

生成每日行程:

```python
for day_index in range(request.travel_days):
```

天气对齐:

```python
for i in range(request.travel_days):
```

预算计算:

```python
total_transportation = max(30 * len(plan.days), 0)
```

为什么限制 `ge=1, le=30`?

```python
ge=1, le=30
```

这是校验规则:

- 至少 1 天。
- 最多 30 天。

如果用户传 0 天或 100 天，FastAPI 会直接拒绝请求，Agent 不需要处理这种明显非法输入。

这就是 Schema Validation。

### 5.4 `transportation`

```python
transportation: str
```

它告诉 Agent 用户偏好的交通方式:

```text
公共交通 / 自驾 / 步行 / 混合
```

目前它主要进入每日计划:

```python
transportation=request.transportation
```

以后可以扩展路线规划节点:

```text
公共交通 -> 调公交路线
自驾 -> 调驾车路线
步行 -> 调步行路线
```

### 5.5 `accommodation`

```python
accommodation: str
```

它影响酒店搜索:

```python
keyword = request.accommodation if "酒店" in request.accommodation else f"{request.accommodation}酒店"
```

比如:

```text
经济型酒店 -> 搜索经济型酒店
豪华酒店 -> 搜索豪华酒店
民宿 -> 搜索民宿酒店
```

还影响预算估算:

```python
经济型酒店 -> 350
舒适型酒店 -> 550
豪华酒店 -> 900
民宿 -> 300
```

### 5.6 `preferences`

```python
preferences: List[str]
```

它是用户旅行偏好:

```text
历史文化、自然风光、美食、购物、艺术、休闲
```

Agent 用它搜索景点:

```python
keywords = request.preferences or ["景点", "博物馆", "公园"]
```

如果用户选择“自然风光”，就优先搜索自然风光相关 POI。

这个字段是 Agent 个性化的入口。

### 5.7 `free_text_input`

```python
free_text_input: Optional[str]
```

这是用户的自由补充，比如:

```text
不要太累
带老人
想看日出
对海鲜过敏
```

当前主要进入总体建议:

```python
if request.free_text_input:
    suggestions.append(f"已考虑额外要求: {request.free_text_input}")
```

以后也可以让 LLM 更细地利用它。

### 5.8 为什么 TripRequest 只放这些字段?

因为 `TripRequest` 应该只放“用户知道、用户需要输入”的内容。

不应该放:

- 景点列表
- 酒店列表
- 天气列表
- 预算
- 图片 URL

这些不是用户输入，而是 Agent 后续自己查出来或生成出来的。

所以职责分工是:

```text
TripRequest: 用户目标和约束
TripPlanningState: Agent 运行过程中的中间数据
TripPlan: 最终规划结果
```

这就是为什么 `TripRequest` 这样设计。

## 6. Agent 的 State 为什么一开始只有 request 和 errors

你特别问到:

```python
initial_state = {
    "request": request,
    "errors": [],
}
```

为什么不是一开始就有:

```python
attractions
hotels
weather_info
plan
```

因为初始时这些东西根本还不存在。

### 6.1 State 是 Agent 工作过程中的“进度表”

初始状态只有用户输入:

```python
{
    "request": request,
    "errors": [],
}
```

执行完搜索景点节点后变成:

```python
{
    "request": request,
    "errors": [],
    "attractions": [...]
}
```

执行完天气节点后变成:

```python
{
    "request": request,
    "errors": [],
    "attractions": [...],
    "weather_info": [...]
}
```

执行完酒店节点后变成:

```python
{
    "request": request,
    "errors": [],
    "attractions": [...],
    "weather_info": [...],
    "hotels": [...]
}
```

最后才有:

```python
{
    "request": request,
    "errors": [],
    "attractions": [...],
    "weather_info": [...],
    "hotels": [...],
    "restaurants": [...],
    "plan": TripPlan(...)
}
```

### 6.2 为什么保留 request?

因为每个节点都需要用户原始需求。

景点节点需要:

```python
request.city
request.preferences
request.travel_days
```

天气节点需要:

```python
request.city
```

酒店节点需要:

```python
request.city
request.accommodation
```

生成计划节点需要完整请求:

```python
request.model_dump()
```

所以 `request` 必须放进 State，并贯穿整个流程。

### 6.3 为什么一开始就有 errors?

`errors` 是 Agent 的“问题记录本”。

任何节点失败或数据为空，都可以写进去:

```python
state.setdefault("errors", []).append("未搜索到景点POI")
```

这样做有几个好处:

1. 不让一个小失败立刻中断整个 Agent。
2. 后续节点可以知道前面发生过什么问题。
3. 生成总体建议时可以提醒用户“部分实时数据不可用”。
4. 兜底方案可以保留错误上下文。

所以初始 State 需要:

```python
"errors": []
```

如果不初始化,每个节点都要判断 errors 是否存在，代码会更乱。

### 6.4 为什么不用空列表初始化所有字段?

比如这样:

```python
{
    "request": request,
    "errors": [],
    "attractions": [],
    "hotels": [],
    "restaurants": [],
    "weather_info": [],
    "plan": None
}
```

也可以，但当前代码选择更轻的写法。

因为 `TripPlanningState` 定义时用了:

```python
class TripPlanningState(TypedDict, total=False):
```

`total=False` 的意思是:

```text
这个字典里的字段不是一开始必须全部存在。
```

也就是说:

```python
state.get("attractions", [])
```

可以安全处理“没有 attractions 字段”的情况。

这正适合 Agent 工作流:

```text
字段随着节点执行逐步出现。
```

## 7. TripPlanningState 和 TripRequest 的区别

很多初学者会混淆这两个。

### 7.1 TripRequest

`TripRequest` 是用户发来的请求。

它只描述用户目标:

```text
我要去哪
什么时候去
去几天
喜欢什么
住什么
怎么走
还有什么要求
```

它来自前端。

### 7.2 TripPlanningState

`TripPlanningState` 是 Agent 执行过程中的内部状态。

它包含:

```text
用户请求
搜索到的景点
查到的天气
查到的酒店
查到的餐饮
生成的计划
执行中的错误
```

它不来自前端，而是 Agent 自己一步步构造出来的。

### 7.3 TripPlan

`TripPlan` 是最终结果。

它用于返回给前端展示。

三者关系:

```text
TripRequest -> TripPlanningState -> TripPlan
```

更准确地说:

```text
TripRequest 是输入
TripPlanningState 是中间工作台
TripPlan 是输出
```

## 8. LangGraph 工作流如何定义

文件:

```text
backend/app/agents/trip_planner_agent.py
```

核心方法:

```python
def _build_graph(self):
```

代码:

```python
workflow = StateGraph(TripPlanningState)
```

这表示:

```text
创建一个基于 TripPlanningState 的 LangGraph 工作流。
```

然后注册节点:

```python
workflow.add_node("search_attractions", self._search_attractions_node)
workflow.add_node("get_weather", self._get_weather_node)
workflow.add_node("search_hotels", self._search_hotels_node)
workflow.add_node("search_restaurants", self._search_restaurants_node)
workflow.add_node("generate_plan", self._generate_plan_node)
workflow.add_node("validate_plan", self._validate_plan_node)
```

再连接节点:

```python
workflow.add_edge(START, "search_attractions")
workflow.add_edge("search_attractions", "get_weather")
workflow.add_edge("get_weather", "search_hotels")
workflow.add_edge("search_hotels", "search_restaurants")
workflow.add_edge("search_restaurants", "generate_plan")
workflow.add_edge("generate_plan", "validate_plan")
workflow.add_edge("validate_plan", END)
```

执行顺序:

```text
START
  -> search_attractions
  -> get_weather
  -> search_hotels
  -> search_restaurants
  -> generate_plan
  -> validate_plan
  -> END
```

这就是工作流型 Agent。

## 9. 节点一: 搜索景点

方法:

```python
_search_attractions_node()
```

输入 State 中已有:

```python
request
errors
```

读取:

```python
request = state["request"]
keywords = request.preferences or ["景点", "博物馆", "公园"]
```

如果用户选了偏好:

```text
自然风光
```

就用它搜索。

如果没选偏好，就用默认关键词。

调用高德:

```python
pois = self.amap_service.search_poi(str(keyword), request.city, limit=12)
```

这里 `amap_service` 来自:

```text
backend/app/services/amap_service.py
```

高德返回的是 POI。

POI 是通用兴趣点，不一定只代表景点，也可能是酒店、餐厅、商场。

所以这里要把 `POIInfo` 转成 `Attraction`:

```python
attractions.append(self._poi_to_attraction(poi, str(keyword)))
```

最后返回:

```python
return {
    "attractions": attractions[: max(6, request.travel_days * 3)],
    "errors": state.get("errors", [])
}
```

LangGraph 会把这个返回值合并进 State。

## 10. POIInfo 为什么要转成 Attraction

文件:

```text
backend/app/models/schemas.py
```

`POIInfo` 是高德搜索结果的简化模型:

```python
class POIInfo(BaseModel):
    id: str
    name: str
    type: str
    address: str
    location: Location
    tel: Optional[str]
```

它表达的是:

```text
高德地图上的一个点
```

而 `Attraction` 是旅行计划里的景点模型:

```python
class Attraction(BaseModel):
    name: str
    address: str
    location: Location
    visit_duration: int
    description: str
    category: Optional[str]
    rating: Optional[float]
    photos: Optional[List[str]]
    poi_id: Optional[str]
    image_url: Optional[str]
    ticket_price: int
```

`Attraction` 比 `POIInfo` 多了旅行相关信息:

- 游览时长
- 景点描述
- 门票价格
- 图片
- POI ID

所以必须转换。

转换函数:

```python
def _poi_to_attraction(poi: POIInfo, keyword: str) -> Attraction:
```

其中最重要的一行:

```python
poi_id=poi.id
```

为什么重要?

因为结果页加载图片时可以用 `poi_id` 再查高德 POI 详情。

这就是后面图片链路的基础。

## 11. 高德服务是 Agent 的工具

文件:

```text
backend/app/services/amap_service.py
```

这个文件不是 Agent，但它是 Agent 的 Tool。

Agent 不直接写 HTTP 请求，而是调用服务方法:

```python
self.amap_service.search_poi(...)
self.amap_service.get_weather(...)
self.amap_service.get_poi_detail(...)
```

### 11.1 通用请求方法 `_get`

```python
def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
```

作用:

```text
统一调用高德 REST API。
```

它会自动加:

```python
"key": self.api_key
"output": "JSON"
```

也就是你配置的:

```env
AMAP_API_KEY=...
```

### 11.2 搜索 POI

```python
def search_poi(self, keywords: str, city: str, citylimit: bool = True, limit: int = 20) -> List[POIInfo]:
```

调用高德:

```text
/place/text
```

把高德原始 JSON 转成:

```python
List[POIInfo]
```

这一步很重要。

如果不转成结构化模型，后面的 Agent 就只能处理混乱的 JSON。

### 11.3 查询天气

```python
def get_weather(self, city: str) -> List[WeatherInfo]:
```

调用:

```text
/weather/weatherInfo
```

把结果转成:

```python
List[WeatherInfo]
```

### 11.4 查询 POI 详情和图片

```python
def get_poi_detail(self, poi_id: str) -> Dict[str, Any]:
```

调用:

```text
/place/detail
```

并提取图片:

```python
detail["photo_urls"] = self._extract_photo_urls(detail)
```

这部分不在 Agent 主工作流里，而是在结果页加载图片时使用。

为什么不放进 Agent 主流程?

因为图片不是生成行程所必需的。

Agent 生成计划只需要:

- 景点名称
- 地址
- 经纬度
- 游览时长
- 描述

图片是展示层增强。

所以项目把图片放到:

```text
结果页按需加载
```

而不是:

```text
规划时一次性查所有图片
```

这样可以减少规划接口耗时。

## 12. 节点二: 查询天气

方法:

```python
_get_weather_node()
```

代码:

```python
weather = self.amap_service.get_weather(request.city)
```

返回:

```python
return {"weather_info": weather, "errors": state.get("errors", [])}
```

为什么天气放进 State?

因为后面的 LLM 生成计划时需要参考天气:

```python
"weather_info": [item.model_dump() for item in self._weather_for_trip_days(state)]
```

虽然当前版本还没有复杂的“下雨改室内景点”逻辑，但数据已经进入 State，后面可以扩展。

## 13. 节点三: 搜索酒店

方法:

```python
_search_hotels_node()
```

用到了:

```python
request.city
request.accommodation
```

构造搜索关键词:

```python
keyword = request.accommodation if "酒店" in request.accommodation else f"{request.accommodation}酒店"
```

搜索高德:

```python
pois = self.amap_service.search_poi(keyword, request.city, limit=10)
```

转换:

```python
hotels = [self._poi_to_hotel(poi, request.accommodation) for poi in pois[:8]]
```

写回 State:

```python
return {"hotels": hotels, "errors": state.get("errors", [])}
```

## 14. 节点四: 搜索餐饮

方法:

```python
_search_restaurants_node()
```

代码:

```python
pois = self.amap_service.search_poi("特色美食 餐厅", request.city, limit=18)
restaurants = [self._poi_to_meal(poi, index) for index, poi in enumerate(pois)]
```

为什么餐饮也要放进 State?

因为最终 `TripPlan` 每天要有三餐:

```python
meals: List[Meal]
```

如果不提前查餐厅，LLM 很容易编不存在的餐厅。

这就是:

```text
让工具提供事实，让 LLM 负责组织。
```

## 15. 节点五: 生成行程

方法:

```python
_generate_plan_node()
```

这是 LLM 介入的地方。

判断:

```python
if self.llm.available and state.get("attractions"):
```

意思是:

```text
LLM 配好了，而且已经有景点数据，才让 LLM 生成。
```

为什么必须有景点?

因为如果没有景点，LLM 只能瞎编。这个项目希望 LLM 基于真实数据工作。

### 15.1 构造 LLM 输入

```python
payload = self._build_llm_payload(state)
schema_hint = self._trip_plan_schema_hint()
```

`payload` 会包含:

```python
{
    "request": request.model_dump(),
    "attractions": [...],
    "hotels": [...],
    "restaurants": [...],
    "weather_info": [...],
    "notes": [...]
}
```

### 15.2 调用 LLM

```python
data = self.llm.chat_json([...])
```

`chat_json` 来自:

```text
backend/app/services/llm_service.py
```

它会调用:

```text
LLM_BASE_URL + /chat/completions
```

使用模型:

```env
LLM_MODEL_ID=...
```

### 15.3 为什么要 JSON

前端需要结构化数据展示:

- 每天有哪些景点
- 每个景点在哪里
- 每天三餐是什么
- 酒店是什么
- 天气是什么
- 预算是多少

如果 LLM 只返回一段自然语言，前端很难展示成卡片、地图和预算。

所以这里要求:

```text
只返回 JSON 对象
```

然后用:

```python
TripPlan(**data)
```

把 JSON 校验成 Pydantic 模型。

## 16. LLM 服务如何工作

文件:

```text
backend/app/services/llm_service.py
```

初始化:

```python
self.api_key = os.getenv("LLM_API_KEY") ...
self.base_url = os.getenv("LLM_BASE_URL") ...
self.model = os.getenv("LLM_MODEL_ID") ...
```

所以你 `.env` 里配置的:

```env
LLM_API_KEY=...
LLM_BASE_URL=...
LLM_MODEL_ID=qwen3.7-plus
```

会在这里被读取。

调用接口:

```python
url = f"{self.base_url}/chat/completions"
```

请求体:

```python
payload = {
    "model": self.model,
    "messages": messages,
    "temperature": temperature,
}
```

如果要求 JSON:

```python
response_format={"type": "json_object"}
```

这就是 OpenAI-compatible API 调用方式。

## 17. 节点六: 校验行程

方法:

```python
_validate_plan_node()
```

检查:

```python
if not isinstance(plan, TripPlan):
    raise ValueError("规划结果不是TripPlan对象")
```

检查天数:

```python
if len(plan.days) != request.travel_days:
    raise ValueError("规划天数与请求不一致")
```

然后标准化:

```python
plan = self._normalize_plan(plan, request)
```

为什么还要标准化?

因为 LLM 可能输出:

- 城市名写错
- 日期不一致
- 某天没三餐
- 预算不准

所以程序要兜回来:

```python
plan.city = request.city
plan.start_date = request.start_date
plan.end_date = request.end_date
plan.budget = self._calculate_budget(plan)
```

这体现了工程化 Agent 的原则:

```text
LLM 生成,程序校验。
```

## 18. 如果 LLM 失败怎么办

方法:

```python
_create_rule_based_plan()
```

如果:

- 没有 LLM Key
- LLM 调用失败
- LLM 返回不是合法 JSON
- 校验失败

就会走规则化规划。

它不靠 LLM，而是用已有 State 数据硬生成:

```python
attractions = state.get("attractions") or self._default_attractions(request)
hotels = state.get("hotels") or self._default_hotels(request)
restaurants = state.get("restaurants") or self._default_restaurants(request)
weather = self._weather_for_trip_days(state)
```

然后按天分配:

```python
for day_index in range(request.travel_days):
```

每天最多取 3 个景点:

```python
day_attractions = attractions[attraction_cursor : attraction_cursor + 3]
```

每天取三餐:

```python
meals = self._pick_daily_meals(restaurants, meal_cursor)
```

最后生成:

```python
TripPlan(...)
```

这就是 Fallback。

## 19. Agent 最终返回什么

最终返回的是:

```python
TripPlan
```

定义在:

```text
backend/app/models/schemas.py
```

核心结构:

```python
class TripPlan(BaseModel):
    city: str
    start_date: str
    end_date: str
    days: List[DayPlan]
    weather_info: List[WeatherInfo]
    overall_suggestions: str
    budget: Optional[Budget]
```

然后路由包装成:

```python
TripPlanResponse(
    success=True,
    message="旅行计划生成成功",
    data=trip_plan
)
```

前端收到后:

```ts
sessionStorage.setItem('tripPlan', JSON.stringify(response.data))
router.push('/result')
```

也就是说:

```text
Agent 生成的数据被存到浏览器 sessionStorage
然后 Result.vue 读取并展示
```

## 20. 结果页和图片为什么不在 Agent 主流程里

文件:

```text
frontend/src/views/Result.vue
```

结果页读取:

```ts
const data = sessionStorage.getItem('tripPlan')
tripPlan.value = JSON.parse(data)
```

然后加载图片:

```ts
await loadAttractionPhotos()
```

图片加载逻辑:

```ts
const params = new URLSearchParams({ name: attraction.name })
if (attraction.poi_id) {
  params.set('poi_id', attraction.poi_id)
}

fetch(`http://localhost:8000/api/poi/photo?${params.toString()}`)
```

后端接口:

```text
backend/app/api/routes/poi.py
```

图片流程:

```text
Result.vue
  -> /api/poi/photo?name=...&poi_id=...
  -> poi.py
  -> amap_service.get_poi_detail(poi_id)
  -> 提取高德 photo_urls
  -> 如果没有,再用 Unsplash
  -> 前端显示图片
```

为什么图片不放在 Agent 主流程里?

因为图片不是规划决策必需数据。

如果规划时就查所有图片，会让 `/api/trip/plan` 更慢。

现在的设计是:

```text
Agent 负责生成行程
结果页负责补充展示图片
```

这是一种职责分离。

## 21. Agent 技术名词对应表

| 名词 | 本项目中对应代码 |
|---|---|
| Agent | `LangGraphTripPlanner` |
| Workflow Agent | `StateGraph` 定义的固定流程 |
| State | `TripPlanningState` |
| Node | `_search_attractions_node` 等节点函数 |
| Edge | `workflow.add_edge(...)` |
| Tool | `AmapService`, `LLMService` |
| Tool Calling | `self.amap_service.search_poi(...)` |
| LLM | `self.llm.chat_json(...)` |
| Prompt | `PLANNER_SYSTEM_PROMPT` |
| Structured Output | 要求 LLM 返回 JSON |
| Schema | `TripRequest`, `TripPlan`, `Attraction` 等 Pydantic 模型 |
| Validation | `TripPlan(**data)`, `_validate_plan_node()` |
| Fallback | `_create_rule_based_plan()`, `_create_fallback_plan()` |
| Memory / Context | `TripPlanningState` 在节点间传递 |
| Single Source of Truth | 用户输入以 `TripRequest` 为准 |
| Post-processing | `_normalize_plan()`, `_calculate_budget()` |

## 22. 用一句话总结这个项目的 Agent

这个项目的 Agent 不是让大模型一次性回答，而是:

```text
用 TripRequest 表达用户目标,
用 TripPlanningState 保存中间过程,
用 LangGraph 控制执行顺序,
用高德 API 获取事实,
用 LLM 生成结构化计划,
用 Pydantic 校验输出,
用规则化逻辑兜底失败。
```

也就是:

```text
Agent = 用户目标 + State + 工具 + LLM + 控制流 + 校验 + 兜底
```

## 23. 你接下来应该怎么读代码

建议按这个顺序:

```text
1. frontend/src/views/Home.vue
   看用户输入如何变成 requestData

2. frontend/src/types/index.ts
   看前端 TripFormData 和 TripPlanResponse

3. frontend/src/services/api.ts
   看请求如何发到 /api/trip/plan

4. backend/app/api/routes/trip.py
   看 FastAPI 如何把 JSON 变成 TripRequest

5. backend/app/models/schemas.py
   看 TripRequest / TripPlan / Attraction / Hotel / Meal 的设计

6. backend/app/agents/trip_planner_agent.py
   看 Agent 如何用 StateGraph 执行多个节点

7. backend/app/services/amap_service.py
   看高德工具如何返回结构化数据

8. backend/app/services/llm_service.py
   看 LLM 如何被调用

9. frontend/src/views/Result.vue
   看 TripPlan 如何展示,图片如何补充加载
```

读的时候不要急着理解所有 UI 细节。你只抓这条主线:

```text
TripFormData -> TripRequest -> TripPlanningState -> TripPlan -> Result.vue
```

把这条线吃透,你就能真正理解这个项目的 Agent。

