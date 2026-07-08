"""Redis cache used inside MCP tool servers."""

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, Optional

from ..config import get_settings
from .redis_compat import create_redis_client


class MCPToolCacheService:
    """Cache raw MCP tool JSON payloads behind the tool boundary."""

    def __init__(self):
        settings = get_settings()
        self.enabled = bool(settings.trip_cache_enabled)
        self.redis_url = settings.redis_url
        self.key_prefix = settings.redis_key_prefix.strip(":") or "trip"
        self.poi_ttl_seconds = settings.trip_cache_poi_ttl_seconds
        self.weather_ttl_seconds = settings.trip_cache_weather_ttl_seconds
        self.detail_ttl_seconds = max(settings.trip_cache_poi_ttl_seconds, 86400)
        self._redis = None

        if not self.enabled:
            return

        try:
            self._redis = create_redis_client(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=1,
                socket_timeout=2,
            )
            self._redis.ping()
            print(f"[OK] MCP Redis工具缓存已启用: {self.redis_url}")
        except Exception as exc:
            self.enabled = False
            self._redis = None
            print(f"[WARN] MCP Redis工具缓存不可用,将直接调用外部工具: {exc}")

    def get_json(self, tool_name: str, params: Dict[str, Any]) -> Optional[Any]:
        """Return cached JSON-compatible data, or None on miss/unavailable."""
        if not self.enabled or not self._redis:
            return None

        key = self._cache_key(tool_name, params)
        try:
            raw = self._redis.get(key)
            if not raw:
                print(f"[MCP CACHE] miss {tool_name}: {key}")
                return None
            print(f"[MCP CACHE] hit {tool_name}: {key}")
            return json.loads(raw)
        except Exception as exc:
            print(f"[WARN] 读取MCP Redis工具缓存失败: {exc}")
            return None

    def set_json(self, tool_name: str, params: Dict[str, Any], data: Any, ttl_seconds: Optional[int] = None) -> None:
        """Store JSON-compatible data if cache is available."""
        if not self.enabled or not self._redis or data in (None, [], {}):
            return

        key = self._cache_key(tool_name, params)
        ttl = ttl_seconds if ttl_seconds is not None else self.ttl_for_tool(tool_name)
        try:
            self._redis.setex(
                key,
                max(int(ttl), 1),
                json.dumps(data, ensure_ascii=False),
            )
            print(f"[MCP CACHE] set {tool_name}: {key}")
        except Exception as exc:
            print(f"[WARN] 写入MCP Redis工具缓存失败: {exc}")

    def ttl_for_tool(self, tool_name: str) -> int:
        if tool_name == "get_weather":
            return self.weather_ttl_seconds
        if tool_name == "get_poi_detail":
            return self.detail_ttl_seconds
        return self.poi_ttl_seconds

    def params_with_query_date(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {
            **params,
            "query_date": datetime.now().strftime("%Y-%m-%d"),
        }

    def _cache_key(self, tool_name: str, params: Dict[str, Any]) -> str:
        normalized = json.dumps(params, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
        return f"{self.key_prefix}:mcp:amap:{tool_name}:{digest}"


_mcp_tool_cache_service: Optional[MCPToolCacheService] = None


def get_mcp_tool_cache_service() -> MCPToolCacheService:
    """Get the shared MCP tool cache service for the current process."""
    global _mcp_tool_cache_service

    if _mcp_tool_cache_service is None:
        _mcp_tool_cache_service = MCPToolCacheService()

    return _mcp_tool_cache_service
