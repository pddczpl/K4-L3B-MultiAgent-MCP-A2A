from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .contracts import Contracts


class EvidenceGateway:
    def __init__(self, endpoint: str, team_api_key: str, contracts: Contracts) -> None:
        self.endpoint = endpoint
        self.team_api_key = team_api_key
        self._contracts = contracts
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None

    async def connect(self) -> None:
        if self._stack is not None:
            await self.close()
        self._stack = AsyncExitStack()
        headers = {"Authorization": f"Bearer {self.team_api_key}"}
        timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
        http_client = await self._stack.enter_async_context(
            httpx2.AsyncClient(headers=headers, timeout=timeout)
        )
        read_stream, write_stream = await self._stack.enter_async_context(
            streamable_http_client(self.endpoint, http_client=http_client)
        )
        self._session = await self._stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass
            self._stack = None
            self._session = None

    async def list_tools(self) -> list[str]:
        if self._session is None:
            await self.connect()
        assert self._session is not None
        response = await self._session.list_tools()
        return sorted(tool.name for tool in response.tools)

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        result = None
        for attempt in range(5):
            try:
                if self._session is None:
                    await self.connect()
                assert self._session is not None
                res = await self._session.call_tool(tool_name, arguments=payload)

                is_err = getattr(res, "is_error", None)
                if is_err is None:
                    is_err = getattr(res, "isError", False)
                if is_err:
                    msg = " ".join(
                        block.text for block in res.content if getattr(block, "text", None)
                    )
                    if tool_name == "get_refund_timeline":
                        raise RuntimeError(f"MCP tool {tool_name} failed: {msg}")
                    if attempt == 4:
                        raise RuntimeError(f"MCP tool {tool_name} failed: {msg or 'unknown error'}")
                    try:
                        await self.connect()
                    except Exception:
                        pass
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue

                result = res
                break
            except Exception as exc:
                if "get_refund_timeline" in str(exc):
                    raise
                if attempt == 4:
                    raise
                try:
                    await self.connect()
                except Exception:
                    pass
                await asyncio.sleep(1.0 * (attempt + 1))
        else:
            raise RuntimeError(f"MCP tool {tool_name} call failed after retries")

        if result is None:
            raise RuntimeError(f"MCP tool {tool_name} returned no result")

        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])

        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    gateway = EvidenceGateway(endpoint, team_api_key, contracts)
    await gateway.connect()
    try:
        yield gateway
    finally:
        await gateway.close()
