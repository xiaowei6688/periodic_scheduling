"""Skill 能力：把可复用的专业知识打包成技能，按需注入 Agent 的系统提示词。

Skill 采用 Codex 约定：一个目录一份 SKILL.md，可选前置 frontmatter：

    skills/<name>/SKILL.md
        ---
        name: 设备检修术语
        description: 电力设备检修规范用语
        ---
        （正文：流程、口径、注意事项……）

框架在装配图时把 AgentSpec.skills 里声明的技能内容拼进该 Agent 的 system_prompt，
从而做到“技能 = 可即插即用的提示词资产”，对所有 Agent 通用。
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .config import Settings


@dataclass(frozen=True)
class Skill:
    """已加载的一条技能。"""

    name: str
    """技能名（目录名或 frontmatter 指定）。"""

    path: Path
    """SKILL.md 的绝对路径。"""

    description: str
    """frontmatter 中的 description，可为空。"""

    content: str
    """去掉 frontmatter 后的正文。"""


def default_skill_roots(
    settings: Settings | None = None,
    *,
    cwd: str | os.PathLike[str] | None = None,
) -> list[Path]:
    """计算技能搜索根目录。

    顺序（先到先得）：
    1. Settings.skill_roots 里显式配置的目录（支持相对路径与 ~）；
    2. 项目目录 <cwd>/skills（仓库自带技能约定位置）；
    3. 约定目录 $CODEX_HOME/skills（未设置时回退 ~/.codex/skills）。

    既支持项目内技能，也复用本机已安装的 Codex 技能。
    """
    settings = settings or Settings()
    base = Path(cwd).resolve() if cwd is not None else Path.cwd()

    roots: list[Path] = []
    for raw in settings.skill_roots:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = base / path
        roots.append(path.resolve())

    roots.append(base / "skills")

    codex_home = os.environ.get("CODEX_HOME")
    codex_root = (
        Path(codex_home) / "skills"
        if codex_home
        else Path.home() / ".codex" / "skills"
    )
    roots.append(codex_root)

    # 去重保序
    seen: set[Path] = set()
    result: list[Path] = []
    for path in roots:
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _parse_skill_md(path: Path, fallback_name: str) -> tuple[str, str, str]:
    """解析 SKILL.md，返回 (name, description, content)。"""
    text = path.read_text(encoding="utf-8")
    name, description = fallback_name, ""
    content = text.strip()
    if text.startswith("---"):
        lines = text.splitlines()
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                for line in lines[1:index]:
                    if ":" in line:
                        key, _, value = line.partition(":")
                        key, value = key.strip().lower(), value.strip()
                        if key == "name":
                            name = value
                        elif key == "description":
                            description = value
                rest = lines[index + 1 :]
                content = "\n".join(rest).strip()
                break
    return name, description, content


def load_skill(
    name_or_path: str,
    *,
    roots: Iterable[str | os.PathLike[str]] | None = None,
    settings: Settings | None = None,
) -> Skill:
    """按名字或路径加载一个技能。

    - 含路径分隔符、或以 .md 结尾、或直接指向文件时，当作路径解析；
    - 否则在 roots（默认为 default_skill_roots(settings)）下找 <root>/<name>/SKILL.md。
    """
    if os.sep in name_or_path or name_or_path.endswith(".md"):
        path = Path(name_or_path).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            raise FileNotFoundError(f"未找到技能文件：{path}")
        name, description, content = _parse_skill_md(path, path.parent.name)
        return Skill(name=name, path=path.resolve(), description=description, content=content)

    search_roots = [Path(r).expanduser() for r in roots] if roots is not None else default_skill_roots(settings)
    for root in search_roots:
        if not root.is_absolute():
            root = Path.cwd() / root
        candidate = root / name_or_path / "SKILL.md"
        if candidate.is_file():
            name, description, content = _parse_skill_md(candidate, name_or_path)
            return Skill(name=name, path=candidate.resolve(), description=description, content=content)
    raise FileNotFoundError(
        f"未找到技能「{name_or_path}」，已搜索："
        + "、".join(str(p) for p in search_roots)
    )


def resolve_skills(
    names: Iterable[str],
    *,
    settings: Settings | None = None,
) -> list[Skill]:
    """加载一批技能（保序、去重），供 AgentSpec.skills 装配时使用。"""
    settings = settings or Settings()
    roots = default_skill_roots(settings)
    result: list[Skill] = []
    seen: set[str] = set()
    for name in names:
        skill = load_skill(name, roots=roots, settings=settings)
        if skill.name not in seen:
            seen.add(skill.name)
            result.append(skill)
    return result


def render_skill_block(skills: Iterable[Skill]) -> str:
    """把已加载技能渲染成注入 system_prompt 的中文提示词块。"""
    lines = [
        "以下是从技能库加载的专业技能，请在完成相关任务时严格遵循：",
    ]
    for skill in skills:
        lines.append("")
        lines.append(f"【技能：{skill.name}】")
        if skill.description:
            lines.append(skill.description)
        if skill.content:
            lines.append(skill.content)
    return "\n".join(lines).strip()


__all__ = [
    "Skill",
    "default_skill_roots",
    "load_skill",
    "resolve_skills",
    "render_skill_block",
]
