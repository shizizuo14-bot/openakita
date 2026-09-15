"""plan/todo 载荷归一化测试。

回归背景（2026-09-15 实测事故）：
MiniMax-M3 收到 ``create_todo`` 后直接套用 Claude Code 的 ``TodoWrite`` 风格输出::

    {"title": "分析 …", "steps": [{"status": "in_progress", "step": "收集 …",
                                   "active-form": "收集 …"}, …]}

而 openakita 的规范字段是 ``task_summary`` + ``steps[].{id, description}``。
由于别名未映射，落盘 plan 与前端 SSE plan 双双失去文本，前端 FloatingPlanBar
渲染成只有 ``1.``~``5.`` 序号和 ``0/5`` 的空壳。

这里锁定：别名解析必须两条路径一致，且任何畸形载荷都不能渲染成空行。
"""

from __future__ import annotations

import pytest

from openakita.tools.handlers import todo_state
from openakita.tools.handlers.plan import PlanHandler, clear_session_todo_state
from openakita.tools.handlers.todo_normalize import (
    coerce_steps_payload,
    normalize_plan_step,
    normalize_step_description,
    normalize_step_id,
    normalize_task_summary,
)
from openakita.tools.handlers.todo_store import TodoStore

# ── 线上真实载荷（源自会话 1789459559396-r2q6mzc 的 chain_timeline）───────
CLAUDE_CODE_STYLE_PARAMS: dict = {
    "title": "分析 OpenAkita 在 Agent 编排框架维度的竞品",
    "steps": [
        {
            "status": "in_progress",
            "step": "收集 OpenAkita 内部架构与 Agent 框架能力资料",
            "active-form": "收集 OpenAkita 内部架构与 Agent 框架能力资料",
        },
        {
            "status": "pending",
            "step": "联网调研主流 Agent 编排框架竞品",
            "active-form": "联网调研主流 Agent 编排框架竞品",
        },
        {
            "status": "pending",
            "step": "整合对比矩阵：能力维度 × 竞品定位",
            "active-form": "整合对比矩阵",
        },
        {
            "status": "pending",
            "step": "提炼 OpenAkita 的差异化优势与战略缺口",
            "active-form": "提炼差异化优势与战略缺口",
        },
        {
            "status": "pending",
            "step": "输出面向内部战略决策的竞品分析文档",
            "active-form": "输出竞品分析文档",
        },
    ],
}

EXPECTED_DESCRIPTIONS = [s["step"] for s in CLAUDE_CODE_STYLE_PARAMS["steps"]]


class _DummyAgent:
    def __init__(self, conversation_id: str):
        self._current_conversation_id = conversation_id
        self._current_session_id = conversation_id


# ── normalize_task_summary ────────────────────────────────────────────


class TestNormalizeTaskSummary:
    @pytest.mark.parametrize(
        "params,expected",
        [
            ({"task_summary": "规范字段"}, "规范字段"),
            ({"taskSummary": "驼峰"}, "驼峰"),
            ({"title": "标题别名"}, "标题别名"),
            ({"summary": "摘要别名"}, "摘要别名"),
            ({"name": "名称别名"}, "名称别名"),
            ({"goal": "目标别名"}, "目标别名"),
            ({"task_summary": "  含空白  "}, "含空白"),
            # 规范字段为空 → 继续向下找别名
            ({"task_summary": "", "title": "回退到 title"}, "回退到 title"),
            # 规范字段是空白串 → 继续向下找
            ({"task_summary": "   ", "title": "非空 title"}, "非空 title"),
        ],
    )
    def test_alias_resolution(self, params, expected):
        assert normalize_task_summary(params) == expected

    @pytest.mark.parametrize("params", [{}, None, {"unknown": "x"}, {"task_summary": 123}])
    def test_returns_empty_when_unresolvable(self, params):
        assert normalize_task_summary(params) == ""

    def test_camel_and_snake_priority_prefers_snake(self):
        # 规范字段优先于别名
        assert normalize_task_summary({"title": "别名", "task_summary": "规范"}) == "规范"


# ── normalize_step_id / description ──────────────────────────────────


class TestNormalizeStepFields:
    def test_id_resolves_aliases_then_falls_back(self):
        assert normalize_step_id({"id": "s1"}, 0) == "s1"
        assert normalize_step_id({"step_id": "s2"}, 0) == "s2"
        assert normalize_step_id({"stepId": "s3"}, 0) == "s3"
        assert normalize_step_id({}, 0) == "step_1"
        assert normalize_step_id({}, 4) == "step_5"

    @pytest.mark.parametrize(
        "step,expected",
        [
            ({"description": "规范描述"}, "规范描述"),
            ({"desc": "desc 别名"}, "desc 别名"),
            ({"step": "Claude Code step 别名"}, "Claude Code step 别名"),
            ({"content": "content 别名"}, "content 别名"),
            ({"text": "text 别名"}, "text 别名"),
            ({"title": "title 别名"}, "title 别名"),
            ({"task": "task 别名"}, "task 别名"),
            ({"name": "name 别名"}, "name 别名"),
            ("纯字符串步骤", "纯字符串步骤"),
            # 高优先级字段为空 → 继续向下找
            ({"description": "", "step": "回退到 step"}, "回退到 step"),
        ],
    )
    def test_description_resolves_aliases(self, step, expected):
        assert normalize_step_description(step, 0) == expected

    def test_description_falls_back_to_id_then_index(self):
        # 既无描述也无 id → 回退 step_<n>，绝不返回空串（否则 UI 渲染空行）
        assert normalize_step_description({}, 0) == "step_1"
        assert normalize_step_description({"id": "sX"}, 0) == "sX"
        assert normalize_step_description({}, 2) == "step_3"

    def test_description_is_never_blank_for_any_shape(self):
        for step in ({}, [], None, 0, {"description": "   "}, ""):
            assert normalize_step_description(step, 1) != ""

    def test_normalize_plan_step_shape(self):
        assert normalize_plan_step({"step": "做某事"}, 2) == {
            "id": "step_3",
            "description": "做某事",
            "status": "pending",
        }


# ── coerce_steps_payload ─────────────────────────────────────────────


class TestCoerceStepsPayload:
    def test_list_passthrough(self):
        payload = [{"description": "a"}]
        assert coerce_steps_payload(payload) is payload

    def test_none_and_blank_yield_empty_list(self):
        assert coerce_steps_payload(None) == []
        assert coerce_steps_payload("") == []
        assert coerce_steps_payload("   ") == []

    def test_json_string(self):
        assert coerce_steps_payload('[{"description": "a"}]') == [{"description": "a"}]

    def test_python_literal_string_with_single_quotes(self):
        """模型常把数组引号化成 Python repr（单引号），json.loads 会失败。"""
        raw = "[{'status': 'pending', 'step': '收集资料'}]"
        assert coerce_steps_payload(raw) == [{"status": "pending", "step": "收集资料"}]

    def test_unparseable_string_returns_none(self):
        assert coerce_steps_payload("这不是 JSON") is None

    def test_non_list_payloads_return_none(self):
        assert coerce_steps_payload({"description": "a"}) is None
        assert coerce_steps_payload('{"a": 1}') is None
        assert coerce_steps_payload(42) is None


# ── 端到端：落盘路径 ─────────────────────────────────────────────────


class TestCreateTodoPersistsAliasedPayload:
    """create_todo 收到 Claude Code 风格参数时，落盘 plan 必须带真实文本。"""

    async def _create(self, monkeypatch, tmp_path, sid, params):
        clear_session_todo_state(sid)
        monkeypatch.setattr(PlanHandler, "_resolve_plan_dir", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(todo_state, "_emit_todo_lifecycle_event", lambda *args: None)
        handler = PlanHandler(_DummyAgent(sid))
        monkeypatch.setattr(handler, "_store", TodoStore(tmp_path / "todo_store.json"))
        result = await handler._create_todo(params)
        return handler, result

    async def test_claude_code_style_payload_keeps_real_text(self, monkeypatch, tmp_path):
        sid = "test-todo-normalize-create-1"
        handler, result = await self._create(
            monkeypatch, tmp_path, sid, dict(CLAUDE_CODE_STYLE_PARAMS)
        )

        assert result.startswith("✅ Created todo")

        plan = handler.get_plan_for(sid)
        assert plan is not None
        # 事故主症状之一：task_summary 变成空串
        assert plan["task_summary"] == "分析 OpenAkita 在 Agent 编排框架维度的竞品"
        # 事故主症状之二：5 个步骤描述全为空
        assert [s["description"] for s in plan["steps"]] == EXPECTED_DESCRIPTIONS

    async def test_canonical_payload_unchanged(self, monkeypatch, tmp_path):
        sid = "test-todo-normalize-create-2"
        handler, _ = await self._create(
            monkeypatch,
            tmp_path,
            sid,
            {
                "task_summary": "规范计划",
                "steps": [
                    {"id": "s1", "description": "第一步"},
                    {"id": "s2", "description": "第二步"},
                ],
            },
        )
        plan = handler.get_plan_for(sid)
        assert plan["task_summary"] == "规范计划"
        assert [s["id"] for s in plan["steps"]] == ["s1", "s2"]
        assert [s["description"] for s in plan["steps"]] == ["第一步", "第二步"]

    async def test_goal_alias_still_works(self, monkeypatch, tmp_path):
        sid = "test-todo-normalize-create-3"
        handler, _ = await self._create(
            monkeypatch, tmp_path, sid, {"goal": "旧别名目标", "steps": [{"description": "一步"}]}
        )
        assert handler.get_plan_for(sid)["task_summary"] == "旧别名目标"

    async def test_stringified_steps_payload_is_coerced(self, monkeypatch, tmp_path):
        """steps 被引号化成 Python 字面量时，不得按字符切分出几百个垃圾步骤。"""
        sid = "test-todo-normalize-create-4"
        handler, _ = await self._create(
            monkeypatch,
            tmp_path,
            sid,
            {
                "title": "字符串化 steps",
                "steps": "[{'step': '甲'}, {'step': '乙'}]",
            },
        )
        plan = handler.get_plan_for(sid)
        assert len(plan["steps"]) == 2
        assert [s["description"] for s in plan["steps"]] == ["甲", "乙"]

    async def test_unparseable_steps_rejected(self, monkeypatch, tmp_path):
        sid = "test-todo-normalize-create-5"
        _, result = await self._create(monkeypatch, tmp_path, sid, {"steps": "不是数组"})
        assert result.startswith("❌")


# ── 落盘前两条路径一致性 ─────────────────────────────────────────────


class TestPathsAgree:
    """落盘 plan 与前端 SSE plan 必须解析出同样的描述文本。"""

    async def test_sse_plan_steps_match_persisted_plan(self, monkeypatch, tmp_path):
        sid = "test-todo-normalize-agree"
        clear_session_todo_state(sid)
        monkeypatch.setattr(PlanHandler, "_resolve_plan_dir", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(todo_state, "_emit_todo_lifecycle_event", lambda *args: None)
        handler = PlanHandler(_DummyAgent(sid))
        monkeypatch.setattr(handler, "_store", TodoStore(tmp_path / "todo_store.json"))

        await handler._create_todo(dict(CLAUDE_CODE_STYLE_PARAMS))
        persisted = handler.get_plan_for(sid)

        # 模拟 _reasoning_runtime 的 SSE 构建（同一套 helper）
        raw_steps = coerce_steps_payload(CLAUDE_CODE_STYLE_PARAMS["steps"]) or []
        sse_steps = [normalize_plan_step(s, i) for i, s in enumerate(raw_steps)]
        sse_summary = normalize_task_summary(CLAUDE_CODE_STYLE_PARAMS)

        assert sse_summary == persisted["task_summary"]
        assert [s["description"] for s in sse_steps] == [
            s["description"] for s in persisted["steps"]
        ]


# ── 消息回放恢复路径 ─────────────────────────────────────────────────


class TestTodoStoreRestore:
    def test_rebuild_from_claude_code_tool_input(self, tmp_path):
        store = TodoStore(tmp_path / "todo_store.json")
        plan = store._rebuild_plan_from_create_todo(dict(CLAUDE_CODE_STYLE_PARAMS))

        assert plan["task_summary"] == "分析 OpenAkita 在 Agent 编排框架维度的竞品"
        assert [s["description"] for s in plan["steps"]] == EXPECTED_DESCRIPTIONS

    def test_rebuild_handles_stringified_steps(self, tmp_path):
        store = TodoStore(tmp_path / "todo_store.json")
        plan = store._rebuild_plan_from_create_todo(
            {"title": "恢复计划", "steps": "[{'step': '甲'}, {'step': '乙'}]"}
        )
        assert plan["task_summary"] == "恢复计划"
        assert [s["description"] for s in plan["steps"]] == ["甲", "乙"]
