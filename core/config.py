"""全局配置。所有环境变量均无前缀（大小写不敏感），见 .env.example。"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from .mcp import McpServer
from .sandbox import SandboxConfig


class Settings(BaseSettings):
    """框架配置。字段名 -> 环境变量名（大小写不敏感）。

    复杂列表字段支持用 JSON 字符串设置环境变量，例如：
        MCP_SERVERS='[{"name":"filesystem","transport":"stdio","command":"npx","args":["-y","..."],"prefix":"fs_"}]'
        SKILL_ROOTS='["skills","/path/to/company/skills"]'
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 模型（OpenAI 兼容协议）----
    llm_provider: str = "openai-compatible"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.0
    llm_enable_thinking: bool = False
    # ---- 检查点持久化 ----
    # PostgreSQL 优先；未配置时可用 SQLite 保存上下文摘要和审批暂停现场。
    postgres_url: str | None = None
    checkpointer_db_path: str | None = None

    # ---- 可观测性 / 成本统计 ----
    # none：关闭；langfuse：接 Langfuse；phoenix：接 Phoenix（OpenInference + OTLP）；langsmith：接 LangSmith。
    observability_provider: str = "none"
    # Langfuse：平台公钥 / 私钥 / 自建服务地址。
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None
    # Phoenix：OTLP Collector 地址与项目名（默认本地 Phoenix 默认端口）。
    phoenix_endpoint: str | None = None
    phoenix_project_name: str = "scheduling"
    # LangSmith：API Key / 项目名 / 服务地址（默认公共云，私有化时配置 endpoint）。
    langsmith_api_key: str | None = None
    langsmith_project: str = "scheduling"
    langsmith_endpoint: str | None = None

    # ---- 审批审计 ----
    # 审批流水 SQLite 文件路径；不填则不落库（仅保留在内存）。生产建议设置。
    approval_db_path: str | None = None
    # 步骤进度 SQLite 文件路径；不填则运行内使用内存存储（进程退出即丢）。生产建议设置。
    step_db_path: str | None = None
    # 结构化 Agent 结果 Artifact 的 SQLite 路径；留空时运行时使用进程内存储。
    artifact_db_path: str | None = None
    # 跨轮临时流程状态；Web API 默认与其他 SQLite Store 共用数据库。
    workflow_state_db_path: str | None = None
    # 面向用户的历史对话存储；API 默认启用 SQLite，裸运行时不设置时使用内存。
    conversation_db_path: str | None = None
    # 历史对话保留天数；留空表示不自动过期。
    conversation_retention_days: int | None = None

    # ---- API 认证 ----
    # Bearer token -> 调用主体。留空时保持本地开发模式（不鉴权）。
    api_keys: dict[str, str] = {}

    # ---- 上下文压缩 ----
    max_context_tokens: int = 30000  # 超过该 token 数即触发总结+裁剪
    keep_recent_messages: int = 6    # 压缩时保留最近 N 条消息

    # ---- 图运行 ----
    recursion_limit: int = 50

    # ---- 总调度 ----
    supervisor_name: str = "supervisor"
    supervisor_prompt: str | None = None

    # ---- Skill ----
    # 技能搜索根目录（相对路径基于进程 cwd）。默认还会额外搜索 $CODEX_HOME/skills。
    skill_roots: list[str] = []

    # ---- MCP 工具 ----
    # 外部 MCP server 连接配置；AgentSpec.mcp_servers 按 server.name 引用。
    mcp_servers: list[McpServer] = []

    # ---- 沙箱执行 ----
    # 云端沙箱供应商配置；AgentSpec.sandboxes 按 provider.name 引用。
    sandbox_providers: list[SandboxConfig] = []
