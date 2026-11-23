"""命令行入口。从源码根目录运行，可自动发现 agents 包。"""
from __future__ import annotations

import argparse
import os
import sys

from .steps import StepEvent

_ICONS = {"in_progress": "▸", "completed": "✓", "failed": "✗"}


def _print_steps(steps: list[StepEvent]) -> None:
    for step in steps:
        icon = _ICONS[step.status]
        line = f"{icon} [{step.agent}] {step.title}"
        if step.detail:
            line = f"{line} —— {step.detail}"
        print(line)


def _ensure_root_on_path() -> None:
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)


def main(argv: list[str] | None = None) -> None:
    _ensure_root_on_path()

    parser = argparse.ArgumentParser(prog="axis-demo", description="axis 多智能体框架示例")
    parser.add_argument("mode", nargs="?", default="quickstart",
                        choices=["quickstart", "chat"], help="quickstart 跑一轮示例；chat 进入多轮对话")
    args = parser.parse_args(argv)

    from core import AgentRegistry, AgentRuntime, Settings, build_runtime

    settings = Settings()
    registry = AgentRegistry().discover_package("agents")
    print(f"已发现 Router：{registry.router_names()}；Agent：{registry.names()}")

    rt: AgentRuntime = build_runtime(
        registry.all(), settings=settings, routers=registry.routers()
    )

    if args.mode == "chat":
        thread_id = input("会话 ID（回车默认 demo）：").strip() or "demo"
        print("输入 quit 退出。\n")
        while True:
            text = input("你 > ").strip()
            if text.lower() in {"quit", "exit", "q"}:
                break
            if not text:
                continue
            try:
                result, steps = rt.invoke_with_steps(text, thread_id=thread_id)
                _print_steps(steps)
                answer = rt.last_text(result)
                if answer:
                    print(f"助手 > {answer}\n")
                else:
                    print("（等待人工审批，请通过 HTTP / WebSocket / API 提交审批决定）\n")
            except Exception as e:  # noqa: BLE001
                print(f"[错误] {e}\n")
    else:
        result, steps = rt.invoke_with_steps(
            "我家附近的主变有点异常，会不会停电？另外帮我看看要不要检修。",
            thread_id="demo-1",
        )
        _print_steps(steps)
        print("\n最终回答：")
        print(rt.last_text(result))


if __name__ == "__main__":
    main()
