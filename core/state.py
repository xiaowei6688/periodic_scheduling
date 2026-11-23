"""共享状态定义。

框架统一使用 LangGraph 的 MessagesState：消息累积由内置 add_messages reducer 负责。
上下文压缩产生的“历史摘要”也作为一条消息写回 messages，从而对所有 Agent 均可见。
"""
from langgraph.graph import MessagesState

# 对外暴露统一的 AgentState 别名，方便后续扩展自定义字段（例如 summary、metadata）。
AgentState = MessagesState
