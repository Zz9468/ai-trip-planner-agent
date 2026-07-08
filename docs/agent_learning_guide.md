# Agent 学习指南

这份指南基于当前项目:

```text
helloagents-trip-planner
```

目标不是让你一开始就看懂每一行代码，而是先建立正确的 Agent 学习顺序。

## 1. 先明确: 你现在学的是 Agent 架构

在这个项目里，Agent 不等于“一个会聊天的大模型”。

更准确地说:

```text
Agent = 用户目标 + State + 工具 + LLM + 工作流 + 校验 + 兜底
```

对应到本项目:

```text
用户目标: TripRequest
State: TripPlanningState
工具: AmapService
LLM: LLMService
工作流: LangGraph StateGraph
校验: Pydantic TripPlan
兜底: _create_rule_based_plan / _create_fallback_plan
```

所以你现在的学习重点不是:

```text
高德 API 每个参数是什么意思
HTTP 请求怎么拼
图片 URL 怎么提取
去重细节怎么写
```

而是:

```text
一个 Agent 如何拆节点
节点如何读写 State
工具什么时候调用
LLM 什么时候调用
输出如何校验
失败如何兜底
```

## 2. 先学架构，再学实现细节

每看到一个函数，先不要问:

```text
每一行代码是什么意思?
```

先问:

```text
这个函数在 Agent 架构里扮演什么角色?
```

比如:

```python
_search_attractions_node()
```

不要先深抠:

```text
keywords[:4] 为什么是 4
limit=12 为什么是 12
seen 怎么去重
```

先理解它的架构角色:

```text
这是一个工具调用节点。
它从 State 读取 request。
它调用高德 search_poi 工具。
它把搜索到的景点写回 State。
```

这才是学 Agent 的重点。

## 3. 看节点时固定问 5 个问题

每个 LangGraph 节点都用这 5 个问题理解:

```text
1. 这个节点从 State 读什么?
2. 这个节点调用什么工具或能力?
3. 这个节点产出什么?
4. 这个节点把什么写回 State?
5. 后续哪个节点会使用它的结果?
```

### 示例: 搜索景点节点

```python
_search_attractions_node()
```

回答:

```text
读什么: state["request"]
调用什么: self.amap_service.search_poi(...)
产出什么: List[Attraction]
写回什么: {"attractions": ..., "errors": ...}
谁使用: generate_plan 节点和 fallback 规则规划
```

这样你就抓住了这个节点的 Agent 意义。

### 示例: 生成计划节点

```python
_generate_plan_node()
```

回答:

```text
读什么: request、attractions、hotels、restaurants、weather_info、errors
调用什么: self.llm.chat_json(...)
产出什么: TripPlan
写回什么: {"plan": ...}
谁使用: validate_plan 节点和最终 API 响应
```

## 4. 哪些必须学，哪些可以先跳过

### 必须学: Agent 骨架

这些是 Agent 的核心，不能跳过:

```python
TripRequest
TripPlanningState
StateGraph
workflow.add_node(...)
workflow.add_edge(...)
self.graph.invoke(initial_state)
return {"xxx": value}
TripPlan(**data)
_create_fallback_plan(...)
```

你必须理解:

```text
TripRequest 是用户输入
TripPlanningState 是 Agent 中间状态
TripPlan 是最终输出
add_node 注册节点
add_edge 定义流程
invoke 启动工作流
return dict 写回 State
TripPlan(**data) 校验 LLM 输出
fallback 保证失败时仍能返回结果
```

### 可以暂缓: 业务实现细节

这些细节可以先不用深抠:

```python
keywords[:4]
limit=12
seen.add(...)
max(6, request.travel_days * 3)
httpx.Client(...)
response.raise_for_status()
_parse_location(...)
_extract_photo_urls(...)
```

它们属于:

```text
搜索策略
API 调用细节
数据清洗细节
性能和体验优化
```

这些可以后面再学，也可以让 AI 辅助实现。

## 5. 具体实现细节可以交给 AI，但架构判断必须你掌握

你可以让 AI 写:

```text
高德接口调用代码
POI 去重逻辑
字段解析逻辑
图片提取逻辑
异常处理
测试样例
```

但你必须能判断 AI 写得是否符合架构。

例如 AI 写出:

```python
return attractions
```

你要知道这不适合 LangGraph 节点，因为节点应该返回:

```python
return {"attractions": attractions}
```

如果 AI 在搜索景点节点里让 LLM 编景点:

```python
self.llm.chat_json("生成几个景点")
```

你要知道这不符合本项目原则:

```text
事实由工具获得，LLM 负责组织。
```

如果 AI 在没搜到景点时直接:

```python
raise Exception("未搜索到景点")
```

你要判断是否合适。当前项目更倾向:

```python
state.setdefault("errors", []).append("未搜索到景点POI")
return {"attractions": [], "errors": ...}
```

因为后面有 fallback。

## 6. AmapService 应该怎么学

文件:

```text
backend/app/services/amap_service.py
```

如果你现在主要学 Agent，`amap_service.py` 不需要逐行深学。

你要先把它看成:

```text
Agent 的工具层 Tool Layer
```

你需要知道它提供哪些工具:

```python
search_poi()
get_weather()
plan_route()
geocode()
get_poi_detail()
```

对应能力:

```text
search_poi: 搜索景点、酒店、餐饮
get_weather: 查询天气
plan_route: 规划路线
geocode: 地址转坐标
get_poi_detail: 查询 POI 详情和图片
```

看这个文件时只问:

```text
这个工具输入是什么?
输出是什么?
哪个 Agent 节点调用它?
```

不要一开始陷入:

```text
HTTP 参数怎么拼
高德字段怎么解析
异常怎么处理
```

这些是后续优化工具质量时再学的内容。

## 7. LLMService 应该怎么学

文件:

```text
backend/app/services/llm_service.py
```

它也不用一开始逐行深学。

你要知道它是:

```text
Agent 的 LLM 调用适配层
```

它提供:

```python
get_llm()
self.llm.available
self.llm.chat(...)
self.llm.chat_json(...)
```

在 Agent 里实际调用发生在:

```python
_generate_plan_node()
```

核心调用:

```python
self.llm.chat_json(...)
```

你需要理解:

```text
LLM_API_KEY 决定有没有模型可用
LLM_BASE_URL 决定请求发到哪里
LLM_MODEL_ID 决定调用哪个模型
chat_json 表示希望模型返回 JSON
```

不需要先深抠:

```text
httpx 怎么请求
OpenAI-compatible 返回结构怎么解析
_extract_json 怎么兼容 Markdown 代码块
```

## 8. 三个核心数据结构必须理解

### 8.1 TripRequest

文件:

```text
backend/app/models/schemas.py
```

它是用户输入:

```text
我要去哪
什么时候去
去几天
怎么出行
住什么
喜欢什么
有什么额外要求
```

它来自前端表单。

### 8.2 TripPlanningState

文件:

```text
backend/app/agents/trip_planner_agent.py
```

它是 Agent 的中间工作台:

```text
request
attractions
hotels
restaurants
weather_info
plan
errors
```

它随着节点执行逐步变大。

初始 State 只有:

```python
{
    "request": request,
    "errors": [],
}
```

原因:

```text
一开始只有用户请求。
景点、天气、酒店、餐饮、计划都还没有生成。
errors 从一开始准备好，方便节点记录问题。
```

### 8.3 TripPlan

文件:

```text
backend/app/models/schemas.py
```

它是最终输出，返回给前端展示。

关系:

```text
TripRequest -> TripPlanningState -> TripPlan
```

即:

```text
输入 -> 中间状态 -> 输出
```

## 9. 这个项目里的 Agent 分工

你可以这样理解:

```text
trip_planner_agent.py
  Agent 大脑和流程

amap_service.py
  Agent 的工具箱，负责真实世界数据

llm_service.py
  Agent 的语言生成器，负责调用大模型

schemas.py
  Agent 的数据契约，规定输入输出格式

routes/trip.py
  Agent 的 API 门面，负责接收前端请求

Home.vue / api.ts
  负责构造 TripRequest

Result.vue
  负责展示 TripPlan，并补充加载图片
```

## 10. 学习顺序建议

不要按文件夹从上到下乱看。

推荐顺序:

```text
1. frontend/src/views/Home.vue
   看用户输入如何形成 requestData

2. frontend/src/services/api.ts
   看 requestData 如何 POST 到 /api/trip/plan

3. backend/app/api/routes/trip.py
   看 FastAPI 如何把 JSON 变成 TripRequest

4. backend/app/models/schemas.py
   理解 TripRequest / TripPlan / Attraction / Hotel / Meal

5. backend/app/agents/trip_planner_agent.py
   理解 StateGraph、节点、State 流动、LLM 调用、fallback

6. backend/app/services/amap_service.py
   只看它暴露了哪些工具能力

7. backend/app/services/llm_service.py
   只看它如何提供 chat_json

8. frontend/src/views/Result.vue
   看 TripPlan 如何被展示
```

## 11. 每段代码的学习模板

以后你给我发代码，我会按这个模板解释:

```text
1. 这段代码在 Agent 架构里的角色
2. 它从 State / 请求中读什么
3. 它调用什么工具或能力
4. 它产出什么
5. 它写回哪里
6. 后续谁使用它
7. 哪些细节可以暂缓
8. 你必须掌握的设计点
```

这比逐行死记更适合学 Agent。

## 12. 你目前最应该掌握的一句话

```text
Agent 节点的本质是:
从 State 读上下文 -> 调用工具或 LLM -> 产出结构化结果 -> 写回 State。
```

这个项目里最重要的模式就是:

```python
def node(state):
    request = state["request"]
    result = tool(...)
    return {"some_key": result}
```

只要你抓住这个模式，再看每个节点都会清楚很多。

## 13. 最小学习目标

第一阶段你不需要能手写整个项目。

你只需要做到:

```text
看到一个节点，能说出它读什么、调什么、写什么。
能画出 TripRequest -> State -> TripPlan 的流程。
知道 LLM 在 generate_plan 节点调用。
知道高德 API 是工具层，不是 Agent 主流程本身。
知道 fallback 为什么存在。
```

做到这些，你就已经入门工程化 Agent 了。

