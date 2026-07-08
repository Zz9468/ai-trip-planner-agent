"""Reusable MCP client wrapper for AMap tools.

The LangGraph nodes are synchronous, so this facade keeps synchronous public
methods while reusing one long-lived stdio MCP session in a worker thread.
"""

import asyncio
import json
import os
import sys
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import get_settings
from ..models.schemas import POIInfo, WeatherInfo


class AmapMCPClient:
    """Sync facade over a reusable AMap MCP stdio session."""

    def __init__(self):
        settings = get_settings()
        self.enabled = bool(settings.use_mcp_tools)
        self.server_module = settings.mcp_amap_server_module
        self.session_start_timeout_seconds = max(int(settings.mcp_session_start_timeout_seconds), 1)
        self.tool_timeout_seconds = max(int(settings.mcp_tool_timeout_seconds), 1)
        self.last_error: Optional[str] = None

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._session: Any = None
        self._ready = threading.Event()
        self._init_error: Optional[BaseException] = None
        self._closed = False
        self._restart_count = 0
        self._lifecycle_lock = threading.Lock()
        self._call_lock = threading.Lock()

    def search_poi(
        self,
        keywords: str,
        city: str,
        citylimit: bool = True,
        limit: int = 20,
    ) -> List[POIInfo]:
        result = self._call_tool(
            "search_poi",
            {
                "keywords": keywords,
                "city": city,
                "citylimit": citylimit,
                "limit": limit,
            },
        )
        if not isinstance(result, list):
            return []
        return [POIInfo.model_validate(item) for item in result]

    def get_weather(self, city: str) -> List[WeatherInfo]:
        result = self._call_tool("get_weather", {"city": city})
        if not isinstance(result, list):
            return []
        return [WeatherInfo.model_validate(item) for item in result]

    def get_poi_detail(self, poi_id: str) -> Dict[str, Any]:
        result = self._call_tool("get_poi_detail", {"poi_id": poi_id})
        return result if isinstance(result, dict) else {}

    def status(self) -> Dict[str, Any]:
        """Return lightweight MCP client runtime status."""
        return {
            "enabled": self.enabled,
            "server_module": self.server_module,
            "connected": self.is_connected,
            "reusable_session": True,
            "restart_count": self._restart_count,
            "tool_timeout_seconds": self.tool_timeout_seconds,
        }

    @property
    def is_connected(self) -> bool:
        thread_alive = bool(self._thread and self._thread.is_alive())
        return bool(self._session and self._loop and thread_alive)

    def close(self) -> None:
        """Close the reusable MCP session, if it has been started."""
        self._stop_session(mark_closed=True)

    def _call_tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        self.last_error = None
        if not self.enabled:
            self.last_error = "MCP tools are disabled"
            return None

        try:
            return self._run_tool_on_reusable_session(name, arguments)
        except Exception as exc:
            self.last_error = str(exc)
            print(f"[WARN] MCP tool {name} failed: {exc}")
            return None

    def _run_tool_on_reusable_session(self, name: str, arguments: Dict[str, Any]) -> Any:
        # ClientSession is not treated as a concurrent tool bus here. Serializing
        # calls keeps the sync facade predictable across FastAPI worker threads.
        with self._call_lock:
            self._ensure_session()
            if not self._loop:
                raise RuntimeError("MCP session loop is not available")

            future = asyncio.run_coroutine_threadsafe(
                self._call_tool_on_session(name, arguments),
                self._loop,
            )
            try:
                return future.result(timeout=self.tool_timeout_seconds)
            except FutureTimeoutError as exc:
                future.cancel()
                self._stop_session(mark_closed=False)
                raise TimeoutError(f"MCP tool {name} timed out after {self.tool_timeout_seconds}s") from exc
            except Exception:
                self._stop_session(mark_closed=False)
                raise

    def _ensure_session(self) -> None:
        if self.is_connected:
            return

        with self._lifecycle_lock:
            if self.is_connected:
                return
            self._start_session_locked()

        if not self._ready.wait(timeout=self.session_start_timeout_seconds):
            self._stop_session(mark_closed=False)
            raise TimeoutError(
                f"MCP session did not start within {self.session_start_timeout_seconds}s"
            )

        if self._init_error:
            error = self._init_error
            self._stop_session(mark_closed=False)
            raise RuntimeError(f"MCP session startup failed: {error}") from error

        if not self.is_connected:
            raise RuntimeError("MCP session started but is not connected")

    def _start_session_locked(self) -> None:
        self._closed = False
        self._init_error = None
        self._session = None
        self._loop = None
        self._stop_event = None
        self._ready.clear()
        self._restart_count += 1
        self._thread = threading.Thread(
            target=self._thread_main,
            name="amap-mcp-session",
            daemon=True,
        )
        self._thread.start()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._session_main())
        except BaseException as exc:
            self._init_error = exc
            self._ready.set()
            if not self._closed:
                print(f"[WARN] MCP session stopped unexpectedly: {exc}")

    async def _session_main(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        backend_dir = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(backend_dir) + os.pathsep + env.get("PYTHONPATH", "")

        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", self.server_module],
            env=env,
            cwd=backend_dir,
        )

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        with self._lifecycle_lock:
            self._loop = loop
            self._stop_event = stop_event

        try:
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    with self._lifecycle_lock:
                        self._session = session
                        self._init_error = None
                    print("[OK] MCP AMap session started and will be reused")
                    self._ready.set()
                    await stop_event.wait()
        except BaseException as exc:
            self._init_error = exc
            self._ready.set()
            if not self._closed:
                print(f"[WARN] MCP session error: {exc}")
            raise
        finally:
            with self._lifecycle_lock:
                self._session = None
                self._loop = None
                self._stop_event = None
            self._ready.set()

    async def _call_tool_on_session(self, name: str, arguments: Dict[str, Any]) -> Any:
        session = self._session
        if not session:
            raise RuntimeError("MCP session is not ready")
        response = await session.call_tool(name, arguments)
        return self._parse_tool_response(response)

    def _stop_session(self, mark_closed: bool) -> None:
        with self._lifecycle_lock:
            if mark_closed:
                self._closed = True
            loop = self._loop
            stop_event = self._stop_event
            thread = self._thread
            had_session = bool(self._session or thread)

        if loop and stop_event and loop.is_running():
            loop.call_soon_threadsafe(stop_event.set)

        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)

        with self._lifecycle_lock:
            if self._thread is thread:
                self._thread = None
            if not thread or not thread.is_alive():
                self._session = None
                self._loop = None
                self._stop_event = None
            self._ready.clear()

        if mark_closed and had_session:
            print("[OK] MCP AMap session closed")

    @staticmethod
    def _parse_tool_response(response: Any) -> Any:
        content = getattr(response, "content", [])
        text_parts: List[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if text:
                text_parts.append(text)

        raw_text = "\n".join(text_parts).strip()
        if not raw_text:
            return None
        return json.loads(raw_text)


_amap_mcp_client: Optional[AmapMCPClient] = None


def get_amap_mcp_client() -> AmapMCPClient:
    """Get a singleton AMap MCP client."""
    global _amap_mcp_client

    if _amap_mcp_client is None:
        _amap_mcp_client = AmapMCPClient()

    return _amap_mcp_client
