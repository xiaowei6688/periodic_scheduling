"""MCP 工具接入：把外部 MCP server 变成框架里可直接挂载的工具。

设计目标（通用、可插拔）：

1. 配置与使用分离：在 Settings.mcp_servers 里声明“有哪些 MCP server”，
   在 AgentSpec.mcp_servers 里声明“哪个专家 Agent 挂载哪些 server 的工具”。
2. 支持主流传输：stdio / SSE / Streamable HTTP / WebSocket。
3. 工具命名默认加 server 前缀（也可自定义），避免多 server 同名工具互相覆盖。

运行期工具加载是异步的（依赖 MCP 协议握手），因此提供异步装配入口
GraphBuilder.abuild() / abuild_runtime()；同步入口在需要 MCP 时会自动兜底。
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal, cast

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import Connection
from pydantic import BaseModel, Field, model_validator

# 与 langchain-mcp-adapters 的 Connection 保持一致。
Transport = Literal["stdio", "sse", "http", "websocket"]


class McpServer(BaseModel):
    """单个 MCP server 的连接配置。

    - transport=stdio：command + args 启动本地进程；
    - transport=sse/http/websocket：url 连接远程服务。
    """

    name: str
    """唯一标识，AgentSpec.mcp_servers 按它引用。"""

    transport: Transport
    """传输方式。"""

    command: str | None = None
    """stdio 专用：启动命令（如 npx / python / uvx）。"""

    args: list[str] = Field(default_factory=list)
    """stdio 专用：命令参数。"""

    url: str | None = None
    """sse/http/websocket 专用：服务地址。"""

    headers: dict[str, Any] | None = None
    """HTTP 类传输：附加请求头（鉴权等）。"""

    env: dict[str, str] | None = None
    """stdio 专用：子进程环境变量（不填则带上一份默认环境）。"""

    cwd: str | None = None
    """stdio 专用：子进程工作目录。"""

    prefix: str | None = None
    """工具名前缀；None 表示默认用 `<server_name>_`。"""

    enabled: bool = True
    """False 时跳过该 server（保留配置但临时停用）。"""

    @model_validator(mode="after")
    def _check_transport(self) -> "McpServer":
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio 传输必须配置 command")
        elif not self.url:
            raise ValueError(f"{self.transport} 传输必须配置 url")
        return self

    def to_connection(self) -> dict[str, Any]:
        """转成 langchain-mcp-adapters 的 Connection 字典。"""
        if self.transport == "stdio":
            conn: dict[str, Any] = {
                "transport": "stdio",
                "command": self.command,
                "args": self.args,
            }
            if self.env is not None:
                conn["env"] = self.env
            if self.cwd is not None:
                conn["cwd"] = str(Path(self.cwd))
            return conn
        transport: Literal["sse", "http", "websocket"] = self.transport
        http_conn: dict[str, Any] = {"transport": transport, "url": self.url}
        if self.headers is not None:
            http_conn["headers"] = self.headers
        return http_conn

    def tool_prefix(self) -> str:
        """该 server 工具应加的前缀（空串表示不前缀）。"""
        if self.prefix is not None:
            return self.prefix
        return f"{self.name}_"


def _prefix_tool(tool: BaseTool, prefix: str) -> BaseTool:
    """给工具改名加前缀；空前缀原样返回。"""
    if not prefix:
        return tool
    renamed = tool.model_copy(update={"name": f"{prefix}{tool.name}"})
    return renamed


async def load_mcp_tools(
    servers: Iterable[McpServer],
    *,
    names: Iterable[str] | None = None,
) -> dict[str, list[BaseTool]]:
    """连接一批 MCP server，返回 {server_name: [工具列表]}。

    - servers：全部可用 server 配置（通常来自 Settings.mcp_servers）。
    - names：本次实际要加载的 server 名（通常来自各 AgentSpec.mcp_servers 并集）；
      为 None 时加载全部。未在 servers 里声明的名字会直接报错，避免静默漏挂。

    返回的工具全部是 LangChain BaseTool，可直接放进 AgentSpec.tools/装配到图上。
    """
    server_list = [s for s in servers if s.enabled]
    if names is not None:
        wanted = list(dict.fromkeys(names))  # 去重且保序
        by_name = {s.name: s for s in server_list}
        unknown = [n for n in wanted if n not in by_name]
        if unknown:
            raise ValueError(
                "以下 MCP server 未在 Settings.mcp_servers 中配置："
                + "、".join(unknown)
                + "。请先配置 MCP_SERVERS 环境变量或在代码中注入。"
            )
        server_list = [by_name[n] for n in wanted]

    client = MultiServerMCPClient(
        cast(dict[str, Connection], {s.name: s.to_connection() for s in server_list}),
        # 统一先不加前缀，下面按每个 server 自己的 prefix 改名，支持逐 server 控制。
        tool_name_prefix=False,
    )
    result: dict[str, list[BaseTool]] = {}
    for server in server_list:
        tools = await client.get_tools(server_name=server.name)
        result[server.name] = [
            _prefix_tool(tool, server.tool_prefix()) for tool in tools
        ]
    return result


async def load_mcp_tools_for_specs(
    servers: Iterable[McpServer],
    server_names_by_spec: Iterable[list[str]],
) -> dict[str, list[BaseTool]]:
    """按多个 Agent 声明的 server 名并集加载工具（去重只连一次）。"""
    wanted: list[str] = []
    for names in server_names_by_spec:
        for name in names:
            if name not in wanted:
                wanted.append(name)
    return await load_mcp_tools(servers, names=wanted)


__all__ = [
    "McpServer",
    "load_mcp_tools",
    "load_mcp_tools_for_specs",
]
