from collections.abc import Iterable
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

from .app import AgentRuntime
from .workflow_state import (
    WorkflowStateStore, MemoryWorkflowStateStore, SqliteWorkflowStateStore,
    PostgresWorkflowStateStore, create_workflow_state_store,
)
from .artifacts import (
    Artifact,
    ArtifactStore,
    artifact_ref,
    MemoryArtifactStore,
    PostgresArtifactStore,
    SqliteArtifactStore,
    create_artifact_store,
    make_artifact_result_tool,
    materialize_artifact_delivery,
    make_publish_artifact_tool,
)
from .approval import (
    ApprovalDecision,
    ApprovalField,
    ApprovalOption,
    ApprovalPolicy,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalSpec,
    ApprovalStore,
    NullApprovalStore,
    PendingApproval,
    REJECTED_PREFIX,
    SqliteApprovalStore,
    PostgresApprovalStore,
    Validator,
    chain_validators,
    is_rejected_output,
    rejection_reason,
    reject_placeholders,
    require_approval,
    required_fields,
    with_execution_policy,
)
from .builder import GraphBuilder, abuild_graph, build_graph
from .config import Settings
from .conversation import (
    ConversationMessage,
    ConversationListPage,
    ConversationSummary,
    ConversationPage,
    ConversationStore,
    MemoryConversationStore,
    PostgresConversationStore,
    SqliteConversationStore,
)
from .facts import (
    FactSpec,
    build_agent_prompt,
    facts_validator,
    render_intake_rules,
)
from .llm import create_chat_model, register_model_provider
from .mcp import McpServer, load_mcp_tools, load_mcp_tools_for_specs
from .messages import last_visible_text, visible_text
from .observability import Observability, build_observability, null_observability
from .memory import (
    create_async_checkpointer,
    create_checkpointer,
    estimate_tokens,
    summarize_and_trim,
)
from .registry import AgentRegistry, AgentSpec, RouterSpec, register, registry
from .sandbox import (
    AlibabaOpenSandboxBackend,
    SandboxBackend,
    SandboxConfig,
    TencentCubeSandboxBackend,
    close_sandbox_backends,
    create_sandbox_backend,
    load_sandbox_backends,
    make_sandbox_tools,
)
from .steps import (
    AsyncStepTracker,
    MemoryStepStore,
    PostgresStepStore,
    NullStepStore,
    SqliteStepStore,
    StepEvent,
    StepSink,
    StepStatus,
    StepStore,
    SyncStepTracker,
    areport_progress,
    report_progress,
    tool_step_title,
    with_progress,
)
from .tools import (
    KnowledgeDocument,
    KnowledgeRetriever,
    calculate,
    current_time,
    make_calculator_tool,
    make_current_time_tool,
    make_knowledge_search_tool,
)
from .skills import (
    Skill,
    default_skill_roots,
    load_skill,
    render_skill_block,
    resolve_skills,
)


def build_runtime(
    specs: Iterable[AgentSpec],
    *,
    model: BaseChatModel | None = None,
    settings: Settings | None = None,
    supervisor_prompt: str | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    approval_store: ApprovalStore | None = None,
    routers: Iterable[RouterSpec] | None = None,
    artifact_store: ArtifactStore | None = None,
    workflow_state_store: WorkflowStateStore | None = None,
    conversation_store: ConversationStore | None = None,
) -> AgentRuntime:
    """一行构建运行时（同步）。specs 为 AgentSpec 列表（或 registry）。"""
    settings = settings or Settings()
    artifact_store = create_artifact_store(settings, artifact_store)
    workflow_state_store = create_workflow_state_store(settings, workflow_state_store)
    model = model or create_chat_model(settings)
    spec_list = list(specs)
    router_list = list(routers or [])
    builder = GraphBuilder(
        spec_list,
        model,
        settings=settings,
        supervisor_prompt=supervisor_prompt,
        routers=router_list,
        artifact_store=artifact_store,
        workflow_state_store=workflow_state_store,
    )
    graph = builder.build(checkpointer=checkpointer)
    return AgentRuntime(
        graph,
        settings=settings,
        approval_store=approval_store,
        sandbox_backends=builder.sandbox_backends,
        agent_names=[
            settings.supervisor_name,
            *(f"router_{router.name}" for router in router_list),
            *(s.name for s in spec_list),
        ],
        agent_display_names={
            **{s.name: s.display_name for s in spec_list if s.display_name},
            **{f"router_{r.name}": r.display_name for r in router_list if r.display_name},
        },
        artifact_store=artifact_store,
        workflow_state_store=workflow_state_store,
        conversation_store=conversation_store,
    )


async def abuild_runtime(
    specs: Iterable[AgentSpec],
    *,
    model: BaseChatModel | None = None,
    settings: Settings | None = None,
    supervisor_prompt: str | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    approval_store: ApprovalStore | None = None,
    routers: Iterable[RouterSpec] | None = None,
    artifact_store: ArtifactStore | None = None,
    workflow_state_store: WorkflowStateStore | None = None,
    conversation_store: ConversationStore | None = None,
) -> AgentRuntime:
    """一行构建运行时（异步）。在 async 应用里推荐使用，MCP 工具会异步加载。"""
    settings = settings or Settings()
    artifact_store = create_artifact_store(settings, artifact_store)
    workflow_state_store = create_workflow_state_store(settings, workflow_state_store)
    model = model or create_chat_model(settings)
    spec_list = list(specs)
    router_list = list(routers or [])
    builder = GraphBuilder(
        spec_list,
        model,
        settings=settings,
        supervisor_prompt=supervisor_prompt,
        routers=router_list,
        artifact_store=artifact_store,
        workflow_state_store=workflow_state_store,
    )
    graph = await builder.abuild(checkpointer=checkpointer)
    return AgentRuntime(
        graph,
        settings=settings,
        approval_store=approval_store,
        sandbox_backends=builder.sandbox_backends,
        agent_names=[
            settings.supervisor_name,
            *(f"router_{router.name}" for router in router_list),
            *(s.name for s in spec_list),
        ],
        agent_display_names={
            **{s.name: s.display_name for s in spec_list if s.display_name},
            **{f"router_{r.name}": r.display_name for r in router_list if r.display_name},
        },
        artifact_store=artifact_store,
        workflow_state_store=workflow_state_store,
        conversation_store=conversation_store,
    )


__all__ = [
    "WorkflowStateStore", "MemoryWorkflowStateStore", "SqliteWorkflowStateStore",
    "PostgresWorkflowStateStore", "create_workflow_state_store",
    "Artifact",
    "ArtifactStore",
    "artifact_ref",
    "MemoryArtifactStore",
    "SqliteArtifactStore",
    "PostgresArtifactStore",
    "materialize_artifact_delivery",
    "ConversationMessage",
    "ConversationListPage",
    "ConversationSummary",
    "ConversationPage",
    "ConversationStore",
    "MemoryConversationStore",
    "SqliteConversationStore",
    "PostgresConversationStore",
    "make_artifact_result_tool",
    "make_publish_artifact_tool",
    "AgentSpec",
    "RouterSpec",
    "AgentRegistry",
    "register",
    "registry",
    "Settings",
    "create_chat_model",
    "register_model_provider",
    "create_checkpointer",
    "create_async_checkpointer",
    "estimate_tokens",
    "last_visible_text",
    "visible_text",
    "summarize_and_trim",
    "GraphBuilder",
    "build_graph",
    "abuild_graph",
    "FactSpec",
    "build_agent_prompt",
    "facts_validator",
    "render_intake_rules",
    "build_runtime",
    "abuild_runtime",
    "AgentRuntime",
    "ApprovalDecision",
    "ApprovalField",
    "ApprovalOption",
    "ApprovalRecord",
    "ApprovalRequest",
    "SqliteApprovalStore",
    "PostgresApprovalStore",
    "ApprovalPolicy",
    "ApprovalSpec",
    "ApprovalStore",
    "NullApprovalStore",
    "REJECTED_PREFIX",
    "is_rejected_output",
    "rejection_reason",
    "PendingApproval",
    "Validator",
    "chain_validators",
    "reject_placeholders",
    "require_approval",
    "required_fields",
    "with_execution_policy",
    "McpServer",
    "load_mcp_tools",
    "load_mcp_tools_for_specs",
    "Skill",
    "default_skill_roots",
    "load_skill",
    "render_skill_block",
    "resolve_skills",
    "SandboxConfig",
    "SandboxBackend",
    "Observability",
    "build_observability",
    "null_observability",
    "AlibabaOpenSandboxBackend",
    "TencentCubeSandboxBackend",
    "create_sandbox_backend",
    "make_sandbox_tools",
    "load_sandbox_backends",
    "close_sandbox_backends",
    "StepEvent",
    "StepStatus",
    "StepStore",
    "MemoryStepStore",
    "NullStepStore",
    "SqliteStepStore",
    "PostgresStepStore",
    "SyncStepTracker",
    "AsyncStepTracker",
    "tool_step_title",
    "with_progress",
    "report_progress",
    "areport_progress",
    "KnowledgeDocument",
    "KnowledgeRetriever",
    "calculate",
    "current_time",
    "make_calculator_tool",
    "make_current_time_tool",
    "make_knowledge_search_tool",
]
