"""POI相关API路由"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional
from ...services.amap_service import get_amap_service
from ...services.unsplash_service import get_unsplash_service

router = APIRouter(prefix="/poi", tags=["POI"])


class POIDetailResponse(BaseModel):
    """POI详情响应"""
    success: bool
    message: str
    data: Optional[dict] = None


@router.get(
    "/detail/{poi_id}",
    response_model=POIDetailResponse,
    summary="获取POI详情",
    description="根据POI ID获取详细信息,包括图片"
)
async def get_poi_detail(poi_id: str):
    """
    获取POI详情
    
    Args:
        poi_id: POI ID
        
    Returns:
        POI详情响应
    """
    try:
        amap_service = get_amap_service()
        
        # 调用高德地图POI详情API
        result = amap_service.get_poi_detail(poi_id)
        
        return POIDetailResponse(
            success=True,
            message="获取POI详情成功",
            data=result
        )
        
    except Exception as e:
        print(f"[ERROR] 获取POI详情失败: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"获取POI详情失败: {str(e)}"
        )


@router.get(
    "/search",
    summary="搜索POI",
    description="根据关键词搜索POI"
)
async def search_poi(keywords: str, city: str = "北京"):
    """
    搜索POI

    Args:
        keywords: 搜索关键词
        city: 城市名称

    Returns:
        搜索结果
    """
    try:
        amap_service = get_amap_service()
        result = amap_service.search_poi(keywords, city)

        return {
            "success": True,
            "message": "搜索成功",
            "data": result
        }

    except Exception as e:
        print(f"[ERROR] 搜索POI失败: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"搜索POI失败: {str(e)}"
        )


@router.get(
    "/photo",
    summary="获取景点图片",
    description="优先根据POI ID从高德获取图片,失败后根据景点名称从Unsplash获取图片"
)
async def get_attraction_photo(name: str, poi_id: Optional[str] = None):
    """
    获取景点图片

    Args:
        name: 景点名称

    Returns:
        图片URL
    """
    try:
        if poi_id:
            amap_service = get_amap_service()
            detail = amap_service.get_poi_detail(poi_id)
            photo_urls = detail.get("photo_urls") or []

            if photo_urls:
                return {
                    "success": True,
                    "message": "获取高德图片成功",
                    "data": {
                        "name": name,
                        "poi_id": poi_id,
                        "photo_url": photo_urls[0],
                        "photo_urls": photo_urls,
                        "source": "amap",
                    }
                }

        unsplash_service = get_unsplash_service()
        # 搜索景点图片
        photo_url = unsplash_service.get_photo_url(f"{name} China landmark")

        if not photo_url:
            # 如果没找到,尝试只用景点名称搜索
            photo_url = unsplash_service.get_photo_url(name)

        return {
            "success": True,
            "message": "获取图片成功",
            "data": {
                "name": name,
                "poi_id": poi_id,
                "photo_url": photo_url,
                "photo_urls": [photo_url] if photo_url else [],
                "source": "unsplash" if photo_url else "placeholder",
            }
        }

    except Exception as e:
        print(f"[ERROR] 获取景点图片失败: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"获取景点图片失败: {str(e)}"
        )
