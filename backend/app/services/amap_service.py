"""高德地图 Web 服务封装."""

from typing import Any, Dict, List, Optional

import httpx

from ..config import get_settings
from ..models.schemas import Location, POIInfo, RouteInfo, WeatherInfo


class AmapService:
    """高德地图 Web 服务封装类."""

    def __init__(self):
        settings = get_settings()
        if not settings.amap_api_key:
            raise ValueError("高德地图API Key未配置,请在.env文件中设置AMAP_API_KEY")

        self.api_key = settings.amap_api_key
        self.base_url = "https://restapi.amap.com/v3"
        self.timeout = 15

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        payload = {**params, "key": self.api_key, "output": "JSON"}
        url = f"{self.base_url}{path}"

        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(url, params=payload)
            response.raise_for_status()
            data = response.json()

        if str(data.get("status")) != "1":
            message = data.get("info") or data.get("infocode") or "未知错误"
            raise RuntimeError(f"高德API调用失败: {message}")

        return data

    @staticmethod
    def _parse_location(value: Any) -> Optional[Location]:
        if not value or not isinstance(value, str) or "," not in value:
            return None

        try:
            longitude, latitude = value.split(",", 1)
            return Location(longitude=float(longitude), latitude=float(latitude))
        except (TypeError, ValueError):
            return None

    def search_poi(self, keywords: str, city: str, citylimit: bool = True, limit: int = 20) -> List[POIInfo]:
        """搜索POI并返回结构化结果."""
        try:
            data = self._get(
                "/place/text",
                {
                    "keywords": keywords,
                    "city": city,
                    "citylimit": "true" if citylimit else "false",
                    "offset": min(max(limit, 1), 25),
                    "page": 1,
                    "extensions": "all",
                },
            )

            pois: List[POIInfo] = []
            for item in data.get("pois", []):
                location = self._parse_location(item.get("location"))
                if not location:
                    continue

                pois.append(
                    POIInfo(
                        id=str(item.get("id") or ""),
                        name=str(item.get("name") or ""),
                        type=str(item.get("type") or ""),
                        address=self._string_value(item.get("address")),
                        location=location,
                        tel=self._string_value(item.get("tel")) or None,
                    )
                )

            return pois

        except Exception as e:
            print(f"[ERROR] POI搜索失败: {str(e)}")
            return []

    def get_weather(self, city: str) -> List[WeatherInfo]:
        """查询城市天气预报."""
        try:
            data = self._get(
                "/weather/weatherInfo",
                {
                    "city": city,
                    "extensions": "all",
                },
            )

            forecasts = data.get("forecasts") or []
            if not forecasts:
                return []

            casts = forecasts[0].get("casts") or []
            weather: List[WeatherInfo] = []
            for item in casts:
                weather.append(
                    WeatherInfo(
                        date=str(item.get("date") or ""),
                        day_weather=str(item.get("dayweather") or ""),
                        night_weather=str(item.get("nightweather") or ""),
                        day_temp=item.get("daytemp") or 0,
                        night_temp=item.get("nighttemp") or 0,
                        wind_direction=str(item.get("daywind") or ""),
                        wind_power=str(item.get("daypower") or ""),
                    )
                )

            return weather

        except Exception as e:
            print(f"[ERROR] 天气查询失败: {str(e)}")
            return []

    def plan_route(
        self,
        origin_address: str,
        destination_address: str,
        origin_city: Optional[str] = None,
        destination_city: Optional[str] = None,
        route_type: str = "walking",
    ) -> Dict[str, Any]:
        """规划路线,返回符合 RouteInfo 的字典."""
        try:
            origin = self.geocode(origin_address, origin_city)
            destination = self.geocode(destination_address, destination_city)
            if not origin or not destination:
                raise ValueError("无法解析起点或终点坐标")

            origin_text = f"{origin.longitude},{origin.latitude}"
            destination_text = f"{destination.longitude},{destination.latitude}"

            if route_type == "driving":
                data = self._get(
                    "/direction/driving",
                    {"origin": origin_text, "destination": destination_text, "extensions": "base"},
                )
                route = (data.get("route") or {}).get("paths", [{}])[0]
                info = self._route_info_from_path(route, route_type)
            elif route_type == "transit":
                data = self._get(
                    "/direction/transit/integrated",
                    {
                        "origin": origin_text,
                        "destination": destination_text,
                        "city": origin_city or destination_city or "",
                        "cityd": destination_city or origin_city or "",
                    },
                )
                transit = (data.get("route") or {}).get("transits", [{}])[0]
                info = RouteInfo(
                    distance=float(transit.get("distance") or 0),
                    duration=int(float(transit.get("duration") or 0)),
                    route_type=route_type,
                    description=self._build_transit_description(transit),
                )
            else:
                data = self._get(
                    "/direction/walking",
                    {"origin": origin_text, "destination": destination_text},
                )
                route = (data.get("route") or {}).get("paths", [{}])[0]
                info = self._route_info_from_path(route, "walking")

            return info.model_dump()

        except Exception as e:
            print(f"[ERROR] 路线规划失败: {str(e)}")
            return RouteInfo(distance=0, duration=0, route_type=route_type, description="路线规划失败").model_dump()

    def geocode(self, address: str, city: Optional[str] = None) -> Optional[Location]:
        """地理编码: 地址转坐标."""
        try:
            params: Dict[str, Any] = {"address": address}
            if city:
                params["city"] = city

            data = self._get("/geocode/geo", params)
            geocodes = data.get("geocodes") or []
            if not geocodes:
                return None

            return self._parse_location(geocodes[0].get("location"))

        except Exception as e:
            print(f"[ERROR] 地理编码失败: {str(e)}")
            return None

    def get_poi_detail(self, poi_id: str) -> Dict[str, Any]:
        """根据 POI ID 获取详情."""
        try:
            data = self._get(
                "/place/detail",
                {
                    "id": poi_id,
                    "extensions": "all",
                },
            )
            pois = data.get("pois") or []
            if not pois:
                return {}

            detail = pois[0]
            detail["photo_urls"] = self._extract_photo_urls(detail)
            return detail

        except Exception as e:
            print(f"[ERROR] 获取POI详情失败: {str(e)}")
            return {}

    @classmethod
    def _extract_photo_urls(cls, value: Any) -> List[str]:
        urls: List[str] = []

        def visit(item: Any):
            if isinstance(item, dict):
                for key, child in item.items():
                    normalized_key = str(key).lower()
                    if normalized_key in {"url", "photo_url", "image_url"} and isinstance(child, str):
                        if child.startswith(("http://", "https://")):
                            urls.append(child)
                    else:
                        visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)

        visit(value)
        return list(dict.fromkeys(urls))

    @staticmethod
    def _route_info_from_path(path: Dict[str, Any], route_type: str) -> RouteInfo:
        steps = path.get("steps") or []
        instructions = [
            str(step.get("instruction") or "").strip()
            for step in steps
            if str(step.get("instruction") or "").strip()
        ]
        description = "；".join(instructions[:5]) or "路线规划成功"

        return RouteInfo(
            distance=float(path.get("distance") or 0),
            duration=int(float(path.get("duration") or 0)),
            route_type=route_type,
            description=description,
        )

    @staticmethod
    def _build_transit_description(transit: Dict[str, Any]) -> str:
        segments = transit.get("segments") or []
        lines: List[str] = []
        for segment in segments:
            buslines = ((segment.get("bus") or {}).get("buslines") or [])
            if buslines:
                name = buslines[0].get("name")
                if name:
                    lines.append(str(name))
                    continue
            walking = segment.get("walking") or {}
            if walking.get("distance"):
                lines.append(f"步行{walking.get('distance')}米")

        return " -> ".join(lines[:6]) or "公交路线规划成功"

    @staticmethod
    def _string_value(value: Any) -> str:
        if value is None or isinstance(value, list) or isinstance(value, dict):
            return ""
        return str(value)


_amap_service: Optional[AmapService] = None


def get_amap_service() -> AmapService:
    """获取高德地图服务实例(单例模式)."""
    global _amap_service

    if _amap_service is None:
        _amap_service = AmapService()

    return _amap_service
