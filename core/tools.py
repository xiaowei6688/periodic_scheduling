"""可选的通用 Agent 工具。

这些工具不会自动挂载到任何 Agent；业务方通过 ``AgentSpec.tools`` 显式选择。
"""
from __future__ import annotations

import ast
import operator
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import BaseTool, StructuredTool

from .steps import with_progress


def current_time(timezone_name: str = "UTC") -> str:
    """获取当前时间。timezone_name 使用 IANA 时区名，例如 Asia/Shanghai。"""
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"不支持的时区：{timezone_name}") from exc
    return datetime.now(timezone.utc).astimezone(zone).isoformat(timespec="seconds")


def make_current_time_tool(
    *,
    default_timezone: str = "UTC",
    name: str = "get_current_time",
    title: str = "查询当前时间",
) -> BaseTool:
    """创建时间工具；可为每个 Agent 设置明确的默认时区。"""
    def get_time(timezone_name: str = default_timezone) -> str:
        return current_time(timezone_name)

    tool = StructuredTool.from_function(
        get_time,
        name=name,
        description="获取当前时间。timezone_name 使用 IANA 时区名，例如 Asia/Shanghai。",
    )
    return with_progress(tool, title=title)


_BINARY_OPS: dict[type[ast.operator], Callable[[Decimal, Decimal], Decimal]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type[ast.unaryop], Callable[[Decimal], Decimal]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _decimal_expression(node: ast.AST) -> Decimal:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    ):
        return Decimal(str(node.value))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_decimal_expression(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
        left = _decimal_expression(node.left)
        right = _decimal_expression(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("幂运算指数不能超过 1000")
        return _BINARY_OPS[type(node.op)](left, right)
    raise ValueError("只支持数字、括号和 + - * / // % ** 运算")


def calculate(expression: str) -> str:
    """精确计算基础算术表达式，不执行任意 Python 代码。"""
    if not expression.strip() or len(expression) > 500:
        raise ValueError("表达式不能为空且不能超过 500 个字符")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ValueError("表达式格式不正确") from exc
    if sum(1 for _ in ast.walk(tree)) > 50:
        raise ValueError("表达式过于复杂")
    try:
        with localcontext() as context:
            context.prec = 50
            result = _decimal_expression(tree.body)
    except (ArithmeticError, InvalidOperation) as exc:
        raise ValueError("表达式无法计算") from exc
    return format(result, "f")


def make_calculator_tool(
    *, name: str = "calculate", title: str = "精确计算"
) -> BaseTool:
    """创建安全的基础算术工具。"""
    tool = StructuredTool.from_function(
        calculate,
        name=name,
        description="精确计算基础算术表达式；支持 + - * / // % ** 和括号，不执行代码。",
    )
    return with_progress(
        tool,
        title=title,
        summary=lambda args: f"计算：{args.get('expression', '')}",
    )


@dataclass(frozen=True)
class KnowledgeDocument:
    """知识检索结果的最小通用格式。"""

    content: str
    title: str = ""
    source: str = ""
    score: float | None = None


class KnowledgeRetriever(Protocol):
    """业务知识库适配器协议，向量库/全文检索服务均可实现。"""

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        filters: Mapping[str, Any] | None = None,
    ) -> Sequence[KnowledgeDocument | Mapping[str, Any] | str]: ...


def _render_document(item: KnowledgeDocument | Mapping[str, Any] | str, index: int) -> str:
    if isinstance(item, KnowledgeDocument):
        title, content, source = item.title, item.content, item.source
    elif isinstance(item, str):
        title, content, source = "", item, ""
    else:
        title = str(item.get("title", ""))
        content = str(item.get("content", item.get("text", "")))
        source = str(item.get("source", ""))
    header = f"{index}. {title}: " if title else f"{index}. "
    suffix = f"（来源：{source}）" if source else ""
    return f"{header}{content[:2000]}{suffix}"


def make_knowledge_search_tool(
    retriever: KnowledgeRetriever,
    *,
    name: str = "search_knowledge",
    title: str = "检索知识库",
) -> BaseTool:
    """将业务知识检索器适配成 Agent 工具。"""
    def search_knowledge(query: str, top_k: int = 5) -> str:
        if not query.strip():
            raise ValueError("检索问题不能为空")
        if not 1 <= top_k <= 20:
            raise ValueError("top_k 必须在 1 到 20 之间")
        documents = retriever.search(query, top_k=top_k)
        if not documents:
            return "未找到相关知识。"
        return "\n".join(_render_document(item, index) for index, item in enumerate(documents, 1))

    tool = StructuredTool.from_function(
        search_knowledge,
        name=name,
        description="检索企业知识库并返回相关资料；不要把机密信息放入 query。",
    )
    return with_progress(
        tool,
        title=title,
        summary=lambda args: f"检索：{args.get('query', '')}",
    )


__all__ = [
    "KnowledgeDocument",
    "KnowledgeRetriever",
    "calculate",
    "current_time",
    "make_calculator_tool",
    "make_current_time_tool",
    "make_knowledge_search_tool",
]
