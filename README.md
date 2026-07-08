# LangGraph智能旅行助手

这是一个基于 LangGraph + FastAPI + Vue 3 的智能旅行规划项目。后端使用 LangGraph 编排旅行规划工作流,通过高德地图 Web 服务获取 POI、天气、地理编码和路线数据,再结合 OpenAI-compatible LLM 生成结构化行程。前端负责收集旅行需求、展示地图/行程/预算,并支持编辑和导出。

## 功能特点

- **LangGraph工作流**: 将旅行规划拆成景点搜索、天气查询、酒店搜索、餐饮搜索、行程生成、结果校验等节点。
- **结构化地图数据**: 高德 Web 服务返回结果会被解析为 Pydantic 模型,不再依赖未解析的文本结果。
- **AI规划与规则兜底**: 有 LLM 时基于真实 POI 数据生成 JSON 行程; LLM 不可用或输出不合格时使用规则化规划兜底。
- **异步任务与SSE进度流**: 旅行规划可以作为后台任务执行,前端通过 SSE 实时展示多 Agent 进度。
- **Redis可选增强**: 支持用 Redis 持久化异步任务状态/事件,并在 MCP Server 内缓存 POI、天气和 POI 详情工具结果。
- **MCP连接复用**: 高德 MCP stdio server 会在首次工具调用时懒启动,后续工具调用复用同一个 session。
- **现代化前端**: Vue 3 + TypeScript + Vite + Ant Design Vue。
- **结果页能力**: 景点地图标记、每日行程、天气、预算、行程编辑、图片/PDF 导出。

## 技术栈

### 后端

- LangGraph
- FastAPI
- Pydantic
- httpx
- Redis
- 高德地图 Web 服务 API
- OpenAI-compatible Chat Completions API

### 前端

- Vue 3 + TypeScript
- Vite
- Ant Design Vue
- 高德地图 JavaScript API
- Axios

## 项目结构

```text
helloagents-trip-planner/
├── backend/
│   ├── app/
│   │   ├── agents/
│   │   │   └── trip_planner_agent.py   # LangGraph旅行规划工作流
│   │   ├── api/
│   │   │   ├── main.py
│   │   │   └── routes/
│   │   ├── services/
│   │   │   ├── amap_service.py         # 高德Web服务结构化封装
│   │   │   ├── llm_service.py          # OpenAI-compatible LLM客户端
│   │   │   ├── mcp_cache_service.py     # MCP Server Redis工具缓存
│   │   │   ├── trip_job_service.py     # 异步任务/SSE/Redis任务状态
│   │   │   └── unsplash_service.py
│   │   ├── models/
│   │   │   └── schemas.py
│   │   └── config.py
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── src/
│   ├── package.json
│   └── .env.example
└── README.md
```

## 快速开始

### 后端

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate  # Windows
pip install -r requirements.txt
copy .env.example .env
```

编辑 `backend/.env`:

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

启动:

```bash
python run.py
```

API 文档: `http://localhost:8000/docs`

### Windows 上启用 Redis

Redis 是可选增强,默认 `TRIP_JOB_BACKEND=auto`: 如果 `REDIS_URL` 可连接,后端会自动使用 Redis; 如果不可连接,会回退到内存版,不影响 `python run.py` 的启动方式。

Windows 本地可以任选一种方式启动 Redis:

```bash
# Docker Desktop
docker run --name trip-redis -p 6379:6379 -d redis:7
```

也可以使用 WSL、Memurai 或 Redis for Windows 兼容服务。项目已兼容 Redis 3.2 这类旧版 Windows Redis,但更推荐 Docker/WSL 中的 Redis 7。启动后保持 `.env` 中:

```env
TRIP_JOB_BACKEND=auto
REDIS_URL=redis://127.0.0.1:6379/0
TRIP_CACHE_ENABLED=true
```

如果想强制要求 Redis,可以设为:

```env
TRIP_JOB_BACKEND=redis
```

如果 Redis 配置了 `requirepass`,需要把密码写入 `REDIS_URL`:

```env
REDIS_URL=redis://:your_redis_password@127.0.0.1:6379/0
```

Redis 生效后:

- `trip:job:{job_id}` 保存异步任务状态和结果。
- `trip:job:{job_id}:events` 保存 Agent 进度事件。
- `trip:mcp:amap:search_poi:*` 缓存景点、酒店、餐饮等 POI 搜索结果。
- `trip:mcp:amap:get_weather:*` 缓存当天城市天气查询结果。
- `trip:mcp:amap:get_poi_detail:*` 缓存 POI 详情结果。

### 前端

```bash
cd frontend
npm install
copy .env.example .env
```

编辑 `frontend/.env`:

```env
VITE_API_BASE_URL=http://127.0.0.1:8000
VITE_AMAP_WEB_JS_KEY=your_amap_js_api_key
```

启动:

```bash
npm run dev
```

访问 `http://localhost:5173`。

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

主要端点:

- `POST /api/trip/plan` - 生成旅行计划
- `POST /api/trip/jobs` - 创建异步旅行规划任务
- `GET /api/trip/jobs/{job_id}` - 查询异步任务状态和结果
- `GET /api/trip/jobs/{job_id}/events` - 监听异步任务 SSE 进度流
- `GET /api/trip/health` - 检查规划工作流
- `GET /api/map/poi` - 搜索 POI
- `GET /api/map/weather` - 查询天气
- `POST /api/map/route` - 规划路线
- `GET /api/poi/photo` - 获取景点图片

## 注意事项

- `AMAP_API_KEY` 使用高德 Web 服务 API Key。
- `VITE_AMAP_WEB_JS_KEY` 使用高德 Web 端 JS API Key。
- 如果未配置 LLM,后端会使用规则化规划兜底,但行程描述质量会下降。
- Unsplash Key 未配置时,景点图片接口会返回空图片 URL,前端会使用占位图。

## 开源协议

CC BY-NC-SA 4.0
