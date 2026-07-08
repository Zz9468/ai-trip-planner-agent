# AI 旅行行程规划 Agent 平台

面向个性化出行场景的智能旅行规划 Agent 平台，基于 LangGraph 编排多节点规划流程，结合高德地图 Web 服务、MCP 工具调用、异步任务进度流和 Vue 前端，生成可展示、可编辑、可导出的结构化旅行行程。

## 项目定位

本项目围绕旅行规划中的目的地信息获取、天气查询、景点筛选、酒店餐饮推荐、路线组织和行程校验进行设计，目标是将真实地图数据与大模型规划能力结合起来，生成更贴近实际出行需求的多日行程方案。

系统支持从用户输入的城市、日期、天数、预算、同行人数和偏好出发，自动完成 POI 检索、天气补充、候选资源组织、行程生成、体验校验和前端展示。

## 核心能力

- 多 Agent 行程规划：基于 LangGraph 将旅行规划拆分为景点搜索、天气查询、酒店搜索、餐饮搜索、行程生成和结果校验等节点。
- 真实地图数据增强：通过高德 Web 服务获取 POI、天气、地理编码和路线数据，并解析为 Pydantic 结构化模型。
- MCP 工具调用：封装高德地图工具为 MCP Server，支持 POI 搜索、天气查询、路线规划和详情查询等能力。
- 异步任务与进度流：旅行规划任务可后台执行，前端通过 SSE 实时展示多 Agent 执行进度。
- 规则兜底机制：LLM 不可用或输出不合格时，自动使用规则化规划生成可用行程，保证系统基本可用性。
- Redis 可选增强：支持用 Redis 持久化任务状态、进度事件和 MCP 工具结果缓存，也可回退到内存模式。
- 可视化结果页：支持景点地图标记、每日行程、天气、预算、行程编辑以及图片/PDF 导出。

## 技术栈

### 后端

- Agent 编排：LangGraph
- Web/API：FastAPI、SSE
- 数据建模：Pydantic
- 外部服务：高德地图 Web 服务 API、OpenAI-compatible Chat Completions API
- 工具协议：MCP、FastMCP
- 任务与缓存：Redis、内存回退
- HTTP 客户端：httpx

### 前端

- Vue 3 + TypeScript
- Vite
- Ant Design Vue
- 高德地图 JavaScript API
- Axios

## 项目结构

```text
ai-trip-planner-agent/
├── backend/
│   ├── app/
│   │   ├── agents/
│   │   │   └── trip_planner_agent.py   # LangGraph 多 Agent 旅行规划工作流
│   │   ├── api/
│   │   │   ├── main.py
│   │   │   └── routes/
│   │   ├── services/
│   │   │   ├── amap_service.py          # 高德 Web 服务结构化封装
│   │   │   ├── llm_service.py           # OpenAI-compatible LLM 客户端
│   │   │   ├── mcp_amap_client.py       # MCP 高德工具客户端
│   │   │   ├── mcp_cache_service.py     # MCP 工具结果缓存
│   │   │   ├── checkpoint_service.py    # LangGraph checkpoint 查询与恢复
│   │   │   ├── trip_job_service.py      # 异步任务/SSE/Redis 任务状态
│   │   │   └── unsplash_service.py
│   │   ├── mcp_servers/
│   │   │   └── amap_server.py           # 高德 MCP Server
│   │   ├── models/
│   │   │   └── schemas.py
│   │   └── config.py
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── src/
│   ├── package.json
│   └── .env.example
├── docs/
└── README.md
```

## 快速开始

### 1. 启动后端

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

编辑 `backend/.env`：

```env
AMAP_API_KEY=your_amap_web_service_key
LLM_API_KEY=your_llm_key
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL_ID=gpt-4o-mini
LLM_TIMEOUT_SECONDS=120
LLM_MAX_RETRIES=2
USE_MCP_TOOLS=true
MCP_SESSION_START_TIMEOUT_SECONDS=10
MCP_TOOL_TIMEOUT_SECONDS=30
TRIP_JOB_BACKEND=auto
REDIS_URL=redis://127.0.0.1:6379/0
TRIP_CACHE_ENABLED=true
```

启动服务：

```powershell
python run.py
```

API 文档地址：

```text
http://localhost:8000/docs
```

### 2. 启动前端

```powershell
cd frontend
npm install
copy .env.example .env
```

编辑 `frontend/.env`：

```env
VITE_API_BASE_URL=http://127.0.0.1:8000
VITE_AMAP_WEB_JS_KEY=your_amap_js_api_key
```

启动前端：

```powershell
npm run dev
```

访问：

```text
http://localhost:5173
```

## Redis 可选增强

Redis 是可选能力，默认 `TRIP_JOB_BACKEND=auto`：

- 如果 `REDIS_URL` 可连接，后端自动使用 Redis。
- 如果 Redis 不可连接，后端回退到内存模式，不影响基础规划流程。

Windows 本地可使用 Docker Desktop 启动 Redis：

```powershell
docker run --name trip-redis -p 6379:6379 -d redis:7
```

Redis 生效后会保存：

- `trip:job:{job_id}`：异步任务状态和结果
- `trip:job:{job_id}:events`：Agent 进度事件
- `trip:mcp:amap:search_poi:*`：景点、酒店、餐饮等 POI 搜索缓存
- `trip:mcp:amap:get_weather:*`：城市天气查询缓存
- `trip:mcp:amap:get_poi_detail:*`：POI 详情缓存

如果想强制使用 Redis：

```env
TRIP_JOB_BACKEND=redis
```

如果 Redis 配置了密码：

```env
REDIS_URL=redis://:your_redis_password@127.0.0.1:6379/0
```

## 核心流程

```text
TripRequest
  -> search_attractions
  -> get_weather
  -> search_hotels
  -> search_restaurants
  -> generate_plan
  -> validate_plan
  -> TripPlanResponse
```

流程说明：

- `TripRequest` 接收城市、日期、天数、人数、预算和偏好。
- `search_attractions` 调用高德工具检索候选景点。
- `get_weather` 补充目的地天气信息。
- `search_hotels` 和 `search_restaurants` 补充住宿与餐饮候选。
- `generate_plan` 基于真实 POI 数据生成每日行程。
- `validate_plan` 校验行程天数、景点分布、预算和体验合理性。

## 主要接口

| 功能 | 方法 | 路径 | 说明 |
| --- | --- | --- | --- |
| 生成旅行计划 | POST | `/api/trip/plan` | 同步生成结构化行程 |
| 创建规划任务 | POST | `/api/trip/jobs` | 创建异步旅行规划任务 |
| 查询任务结果 | GET | `/api/trip/jobs/{job_id}` | 查询异步任务状态和结果 |
| 监听任务进度 | GET | `/api/trip/jobs/{job_id}/events` | SSE 监听 Agent 执行进度 |
| 检查规划服务 | GET | `/api/trip/health` | 检查规划工作流状态 |
| 搜索 POI | GET | `/api/map/poi` | 搜索景点、酒店、餐饮等地点 |
| 查询天气 | GET | `/api/map/weather` | 查询城市天气 |
| 路线规划 | POST | `/api/map/route` | 规划地点之间的路线 |
| 获取图片 | GET | `/api/poi/photo` | 获取景点图片 |

## 常用命令

```powershell
# 后端
cd backend
.\.venv\Scripts\activate
python run.py

# 前端
cd frontend
npm run dev

# 前端构建
npm run build
```

## 注意事项

- `AMAP_API_KEY` 使用高德 Web 服务 API Key。
- `VITE_AMAP_WEB_JS_KEY` 使用高德 Web 端 JS API Key。
- 如果未配置 LLM，后端会使用规则化规划兜底，但行程描述质量会下降。
- Unsplash Key 未配置时，景点图片接口会返回空图片 URL，前端会使用占位图。
- Redis 不是必需服务，不启动 Redis 也可以运行基础规划流程。

## 开源协议

CC BY-NC-SA 4.0
