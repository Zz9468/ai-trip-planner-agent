"""Compatibility helpers for old local Redis servers on Windows."""

from typing import Any, Mapping


def create_redis_client(redis_url: str, **kwargs: Any) -> Any:
    """Create a Redis client that can talk to Redis 3.x.

    redis-py 8 defaults to RESP3 and sends HELLO during connection setup.
    Redis 3.2 does not support HELLO, so force RESP2 here.
    """
    from redis import Redis

    return Redis.from_url(
        redis_url,
        protocol=2,
        driver_info=None,
        **kwargs,
    )


def set_hash_fields(redis_client: Any, key: str, mapping: Mapping[str, Any]) -> None:
    """Set multiple hash fields with Redis 3.x-compatible HMSET."""
    if not mapping:
        return

    args = []
    for field, value in mapping.items():
        args.extend([field, "" if value is None else value])
    redis_client.execute_command("HMSET", key, *args)
