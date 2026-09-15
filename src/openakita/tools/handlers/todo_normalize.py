"""plan / todo 载荷的共享归一化（单一事实来源）。

## 为什么需要这个模块

模型经常不按 ``create_todo`` 的规范 schema 输出，而是直接套用 **Claude Code 的
``TodoWrite`` 习惯**：

.. code-block:: json

    {"title": "…", "steps": [{"step": "…", "active-form": "…", "status": "pending"}]}

而 openakita 的规范字段是 ``task_summary`` + ``steps[].{id, description}``。

同一条 plan 数据有**两个**消费者，各自做了不完整的别名解析：

1. ``todo_handler._create_todo`` —— 落盘 / 后端 plan（只认 ``description``，缺失时回退成 ``id``）
2. ``core/_reasoning_runtime.py`` —— 推给前端的 SSE ``todo_created`` 事件（只认 ``description``）

两边一旦解析不一致，前端卡片就会渲染成一片空白（只有 ``1.`` ``2.`` 序号和 ``0/N``）。
所以别名解析必须**共用同一份实现**，避免再次漂移。

本模块刻意保持零依赖（不 import openakita 内任何东西），
这样 ``tools.handlers`` 与 ``core`` 两侧都能安全引用。
"""

from __future__ import annotations

from typing import Any

# 规范名在最前，其余为容错别名（顺序即优先级）。
TASK_SUMMARY_KEYS: tuple[str, ...] = (
    "task_summary",
    "taskSummary",
    "title",
    "summary",
    "name",
    "goal",
)

STEP_ID_KEYS: tuple[str, ...] = (
    "id",
    "step_id",
    "stepId",
)

STEP_DESCRIPTION_KEYS: tuple[str, ...] = (
    "description",
    "desc",
    "step",  # Claude Code TodoWrite 风格
    "content",  # create_plan_file / todos[] 风格
    "text",
    "title",
    "task",
    "name",
)

# 描述长度上限，与 ``_create_todo`` 既有的 512 截断保持一致。
_DESCRIPTION_MAX_LEN = 512
_TASK_SUMMARY_MAX_LEN = 512
_STEP_ID_MAX_LEN = 64


def _pick_text(container: dict[str, Any], keys: tuple[str, ...]) -> str:
    """返回 ``keys`` 中第一个非空白字符串值（已 strip）。"""
    for key in keys:
        value = container.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def normalize_task_summary(params: dict[str, Any] | None) -> str:
    """解析任务摘要，兼容 ``title`` / ``summary`` / ``name`` / ``goal`` 等别名。"""
    if not isinstance(params, dict):
        return ""
    return _pick_text(params, TASK_SUMMARY_KEYS)[:_TASK_SUMMARY_MAX_LEN]


def normalize_step_id(step: Any, index: int) -> str:
    """解析步骤 ID，缺失时回退为 ``step_<n>``（``index`` 从 0 开始）。"""
    if isinstance(step, dict):
        value = _pick_text(step, STEP_ID_KEYS)
        if value:
            return value[:_STEP_ID_MAX_LEN]
    return f"step_{index + 1}"


def normalize_step_description(step: Any, index: int) -> str:
    """解析步骤描述。

    优先级：规范名 → 别名（含 ``step`` / ``content`` / ``task`` 等）
    → 步骤 ID → ``step_<n>``。

    最后的兜底保证**任何**畸形载荷都不会在 UI 上渲染成空白行。
    """
    if isinstance(step, str):
        return step.strip()[:_DESCRIPTION_MAX_LEN] or f"step_{index + 1}"
    if isinstance(step, dict):
        text = _pick_text(step, STEP_DESCRIPTION_KEYS)
        if text:
            return text[:_DESCRIPTION_MAX_LEN]
    return normalize_step_id(step, index)


def normalize_plan_step(step: Any, index: int, *, status: str = "pending") -> dict[str, str]:
    """把任意形状的 step 归一化成 ``{id, description, status}``。

    用于 SSE ``todo_created`` 事件（前端 ChatTodoStep 形状）。
    落盘路径请直接用 ``todo_handler``，因为它还需要保留 ``tool`` / ``skills``
    等附加字段。
    """
    return {
        "id": normalize_step_id(step, index),
        "description": normalize_step_description(step, index),
        "status": status,
    }


def coerce_steps_payload(raw: Any) -> list[Any] | None:
    """把 ``steps`` / ``todos`` 载荷强制成列表。

    返回：

    - ``list`` —— 可直接迭代的步骤序列（``None`` 入参返回 ``[]``）
    - ``None`` —— 载荷无法解析为列表，调用方应视为「无有效步骤」并报错

    为什么要单独做这件事：模型常把数组**引号化**成字符串再传进来
    （``"[{...}]"``，甚至是 Python 单引号字面量 ``"[{'step': '…'}]"``）。
    直接 ``enumerate`` 一个字符串会**按字符切分**，产出几百个垃圾步骤。
    这里先按 JSON 解析，失败再退回 ``ast.literal_eval`` 兼容 Python 字面量。
    """
    import ast
    import json

    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, TypeError, SyntaxError):
                return None
        return parsed if isinstance(parsed, list) else None
    return None


__all__ = [
    "TASK_SUMMARY_KEYS",
    "STEP_ID_KEYS",
    "STEP_DESCRIPTION_KEYS",
    "normalize_task_summary",
    "normalize_step_id",
    "normalize_step_description",
    "normalize_plan_step",
    "coerce_steps_payload",
]
