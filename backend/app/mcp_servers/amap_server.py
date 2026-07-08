"""MCP server exposing AMap tools for the trip planner agent."""

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from ..services.amap_service import get_amap_service
from ..services.mcp_cache_service import get_mcp_tool_cache_service

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)

mcp = FastMCP("helloagents-amap")


def _json(value: Any) -> str:
    """Return JSON text so the MCP client can parse tool results reliably."""
    return json.dumps(value, ensure_ascii=False)


@mcp.tool()
def search_poi(keywords: str, city: str, citylimit: bool = True, limit: int = 20) -> str:
    """Search AMap POIs by keyword and city."""
    cache = get_mcp_tool_cache_service()
    params = {
        "keywords": keywords,
        "city": city,
        "citylimit": citylimit,
        "limit": limit,
    }
    cached = cache.get_json("search_poi", params)
    if cached is not None:
        return _json(cached)

    service = get_amap_service()
    pois = service.search_poi(keywords=keywords, city=city, citylimit=citylimit, limit=limit)
    data = [poi.model_dump() for poi in pois]
    cache.set_json("search_poi", params, data)
    return _json(data)


@mcp.tool()
def get_weather(city: str) -> str:
    """Get AMap weather forecast for a city."""
    cache = get_mcp_tool_cache_service()
    params = cache.params_with_query_date({"city": city})
    cached = cache.get_json("get_weather", params)
    if cached is not None:
        return _json(cached)

    service = get_amap_service()
    weather = service.get_weather(city)
    data = [item.model_dump() for item in weather]
    cache.set_json("get_weather", params, data)
    return _json(data)


@mcp.tool()
def get_poi_detail(poi_id: str) -> str:
    """Get AMap POI detail by POI id."""
    cache = get_mcp_tool_cache_service()
    params = {"poi_id": poi_id}
    cached = cache.get_json("get_poi_detail", params)
    if cached is not None:
        return _json(cached)

    service = get_amap_service()
    data = service.get_poi_detail(poi_id)
    cache.set_json("get_poi_detail", params, data)
    return _json(data)


if __name__ == "__main__":
    mcp.run()
