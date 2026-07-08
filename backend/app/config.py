"""配置管理模块"""

import os
from typing import List
from pydantic_settings import BaseSettings
from dotenv import load_dotenv

# 加载当前目录的.env
load_dotenv()


class Settings(BaseSettings):
    """应用配置"""

    # 应用基本配置
    app_name: str = "LangGraph智能旅行助手"
    app_version: str = "1.0.0"
    debug: bool = False

    # 服务器配置
    host: str = "0.0.0.0"
    port: int = 8000

    # CORS配置 - 使用字符串,在代码中分割
    cors_origins: str = "http://localhost:5173,http://localhost:3000,http://127.0.0.1:5173,http://127.0.0.1:3000"

    # 高德地图API配置
    amap_api_key: str = ""

    # MCP工具配置
    use_mcp_tools: bool = True
    mcp_amap_server_module: str = "app.mcp_servers.amap_server"
    mcp_session_start_timeout_seconds: int = 10
    mcp_tool_timeout_seconds: int = 30

    # Unsplash API配置
    unsplash_access_key: str = ""
    unsplash_secret_key: str = ""

    # LLM配置 (OpenAI-compatible)
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4"

    # Redis配置
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_key_prefix: str = "trip"
    trip_job_backend: str = "auto"  # auto / redis / memory
    trip_job_ttl_seconds: int = 86400
    trip_job_sse_heartbeat_seconds: int = 15
    trip_cache_enabled: bool = True
    trip_cache_poi_ttl_seconds: int = 604800
    trip_cache_weather_ttl_seconds: int = 21600

    # LangGraph Checkpoint / 持久化状态
    langgraph_checkpoint_enabled: bool = True
    langgraph_checkpoint_backend: str = "sqlite"
    langgraph_checkpoint_db: str = "./data/langgraph_checkpoints.sqlite"

    # 日志配置
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"  # 忽略额外的环境变量

    def get_cors_origins_list(self) -> List[str]:
        """获取CORS origins列表"""
        return [origin.strip() for origin in self.cors_origins.split(',')]


# 创建全局配置实例
settings = Settings()


def get_settings() -> Settings:
    """获取配置实例"""
    return settings


# 验证必要的配置
def validate_config():
    """验证配置是否完整"""
    errors = []
    warnings = []

    if not settings.amap_api_key:
        errors.append("AMAP_API_KEY未配置")

    # 支持 LLM_API_KEY 或 OPENAI_API_KEY
    llm_api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not llm_api_key:
        warnings.append("LLM_API_KEY或OPENAI_API_KEY未配置,LLM功能可能无法使用")

    if errors:
        error_msg = "配置错误:\n" + "\n".join(f"  - {e}" for e in errors)
        raise ValueError(error_msg)

    if warnings:
        print("\n[WARN] 配置警告:")
        for w in warnings:
            print(f"  - {w}")

    return True


# 打印配置信息(用于调试)
def print_config():
    """打印当前配置(隐藏敏感信息)"""
    print(f"应用名称: {settings.app_name}")
    print(f"版本: {settings.app_version}")
    print(f"服务器: {settings.host}:{settings.port}")
    print(f"高德地图API Key: {'已配置' if settings.amap_api_key else '未配置'}")

    # 检查LLM配置
    llm_api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
    llm_base_url = os.getenv("LLM_BASE_URL") or settings.openai_base_url
    llm_model = os.getenv("LLM_MODEL_ID") or settings.openai_model

    print(f"LLM API Key: {'已配置' if llm_api_key else '未配置'}")
    print(f"LLM Base URL: {llm_base_url}")
    print(f"LLM Model: {llm_model}")
    print(f"MCP工具: {'启用' if settings.use_mcp_tools else '关闭'}")
    print(f"MCP Server: {settings.mcp_amap_server_module}")
    print(f"MCP工具超时: {settings.mcp_tool_timeout_seconds}s")
    print(f"异步任务后端: {settings.trip_job_backend}")
    print(f"Redis URL: {settings.redis_url}")
    print(f"MCP工具缓存: {'启用' if settings.trip_cache_enabled else '关闭'}")
    print(f"LangGraph Checkpoint: {'启用' if settings.langgraph_checkpoint_enabled else '关闭'}")
    print(f"LangGraph Checkpoint DB: {settings.langgraph_checkpoint_db}")
    print(f"日志级别: {settings.log_level}")
