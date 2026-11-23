"""用户可见文本提取。

框架内部会把工具调用结果（ToolMessage）、系统消息、以及“工具无输出”的
英文占位补白写回消息历史。这些是给模型看的过程信息，不应直接展示给用户。
这里提供一个统一的过滤/格式化函数，HTTP、WebSocket、CLI 都复用同一套规则，
避免不同出口表现不一致。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

# 已知的“工具无输出”类英文占位文案。即使上游运行时补上这类信息，也不展示给用户。
_HIDDEN_CONTENTS = {
    "Tool ran without output or errors",
    "Tool ran without output or errors.",
}


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)


def visible_text(message: BaseMessage) -> str:
    """返回单条消息的用户可见文本；非 AI 消息或内部过程消息返回空串。

    流式场景下多段 chunk 会前后拼接，故这里**不 strip**，保留原始空白，
    避免把 “Hello ” + “world” 拼成 “Helloworld”。

    以下内容不展示给用户：
    - Tool/System 等内部消息；
    - 内部英文占位补白；
    - 带工具调用的 AI 消息。这类消息是 Agent 的“决策/动作生成”过程，真正面向用户的
      结论会在工具执行后由 Agent 另行输出；若连工具调用前的铺垫也展示，审批类动作
      会先输出“正在创建…”，待审批恢复后又输出“已完成…”，看起来像同一时间连续冒出两条。
    """
    if not isinstance(message, AIMessage):
        return ""
    if getattr(message, "tool_calls", None):
        return ""
    text = _content_to_text(getattr(message, "content", None))
    if not text.strip():
        return ""
    if text.strip() in _HIDDEN_CONTENTS:
        return ""
    return text


def last_visible_text(messages: Sequence[BaseMessage]) -> str:
    """从后往前找最后一条用户可见的 AI 文本，跳过 Tool/System 等内部消息。"""
    for message in reversed(messages):
        text = visible_text(message)
        if text:
            return text.strip()
    return ""
