# MCP 改造说明

本项目已将高德地图工具层改造成 MCP 调用路径，同时保留原来的本地 Python 服务作为兜底。

## 改造前

Agent 节点直接调用本地服务:

```text
LangGraphTripPlanner
  -> AmapService.search_poi()
  -> AmapService.get_weather()
  -> 高德 Web API
```

## 改造后

Agent 节点优先调用 MCP 工具:

```text
LangGraphTripPlanner
  -> AmapMCPClient
  -> AMap MCP Server
  -> AmapService
  -> 高德 Web API
```

如果 MCP 调用失败,Agent 会自动回退:

```text
LangGraphTripPlanner
  -> AmapService
  -> 高德 Web API
```

这样做的目的是让项目具备 MCP 工具调用结构,同时不破坏原来已经跑通的功能。

## 新增文件

- `backend/app/mcp_servers/amap_server.py`
  - MCP Server。
  - 暴露 `search_poi`、`get_weather`、`get_poi_detail` 三个工具。
  - 内部复用原有 `AmapService`,不重复写 HTTP 请求逻辑。

- `backend/app/services/mcp_amap_client.py`
  - MCP Client 封装。
  - 给同步 LangGraph 节点提供 `search_poi`、`get_weather`、`get_poi_detail` 方法。
  - 负责启动 stdio MCP server、调用工具、解析 JSON、转成 Pydantic 模型。

## 修改文件

- `backend/app/agents/trip_planner_agent.py`
  - 新增 `self.amap_mcp`。
  - 新增 `_search_poi()` 和 `_get_weather()` helper。
  - 景点、天气、酒店、餐饮节点改为优先走 MCP。

- `backend/app/api/routes/map.py`
  - 修复健康检查里引用不存在 `service.mcp_tool` 的问题。
  - 返回 MCP 是否启用和 MCP server module。

- `backend/requirements.txt`
  - 新增 `mcp>=1.2.0`。

- `backend/.env.example`
  - 新增:

```env
USE_MCP_TOOLS=true
MCP_AMAP_SERVER_MODULE=app.mcp_servers.amap_server
```

## 学习重点

这次改造要理解的是:

```text
MCP 化的对象不是整个 Agent,而是工具层。
```

也就是说:

```text
LangGraph 仍然负责工作流编排;
LLM 仍然负责生成结构化旅行计划;
MCP 负责把外部能力封装成标准工具;
高德 API 仍然是真实数据来源。
```

在这个项目里,MCP 的位置是:

```text
Agent 节点与高德 API 之间的工具协议层。
```

## 自检方式

在 `backend` 目录执行:

```powershell
pip install -r requirements.txt
python -m py_compile app/agents/trip_planner_agent.py app/services/mcp_amap_client.py app/mcp_servers/amap_server.py app/api/routes/map.py
python -m app.mcp_servers.amap_server
```

最后一个命令会启动 MCP stdio server,通常会等待客户端连接;能正常启动且不报 import 错误即可。
