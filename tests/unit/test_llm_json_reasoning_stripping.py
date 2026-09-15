"""Regression tests: reasoning wrappers must not leak into parsed JSON.

MiniMax-M3 (and Qwen) prefix their answer with a ``<think>...</think>`` block;
Claude uses ``<thinking>``. Models routinely restate the *target schema* inside
that block while reasoning, e.g. ``[{"type": "fact", "content": "..."}]``.

The old ``memory_nudge`` parser ran a non-greedy regex
``r"\\[\\s*(?:\\{.*?\\}\\s*,?\\s*)*\\]"`` over the **raw** response, so it
grabbed that schema example instead of the real answer and persisted the
placeholder text as long-term memory.

The shared helpers in :mod:`openakita.memory.json_utils` now strip reasoning
wrappers first, which fixes every consumer of ``loads_llm_json`` /
``extract_json_array`` / ``extract_json_object`` at once.

See also ``tests/unit/test_intent_prompt_contract.py`` for the same root cause
on the intent-analyzer path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openakita.memory.json_utils import (
    _clean_llm_json_text,
    _strip_reasoning_blocks,
    extract_json_array,
    extract_json_object,
    loads_llm_json,
)

# Shape captured from a live MiniMax-M3 call on 2026-09-15 (openakita 1.27.42,
# endpoint ``minimax-cn``). The model *does* close the ``</think>`` tag.
MINIMAX_WRAPPED = (
    "<think>The user is asking me to extract memory-worthy facts from this "
    "conversation. Let me analyze what's happening:\n\n"
    "1. The user asked for a competitive analysis of \"OpenAkita\"\n"
    "2. The assistant asked about comparison dimensions\n\n"
    "Let me create the JSON array.</think>\n\n"
    "[\n"
    '  {\n    "type": "context",\n'
    '    "content": "用户在做 OpenAkita 的竞品分析",\n'
    '    "importance": 3\n  },\n'
    '  {\n    "type": "preference",\n'
    '    "content": "用户倾向于从架构、扩展性、渠道集成三个维度做竞品对比",\n'
    '    "importance": 3\n  }\n'
    "]"
)

# The pathological case: the model restates the requested schema while
# reasoning. A "first [ ... ]" scan over the raw text picks this up.
REASONING_CONTAINS_SCHEMA_EXAMPLE = (
    "<think>The user asked how memory works. The expected format is:\n"
    '[{"type": "fact", "content": "EXAMPLE_NOT_REAL", "importance": 5}]\n'
    "Nothing else in this conversation is worth saving.</think>\n\n"
    '[{"type": "preference", "content": "USER_LIKES_LIQUID_COOLING",'
    ' "importance": 4}]'
)

CLAUDE_WRAPPED = (
    "<thinking>Let me think about the schema.\n"
    'Example: {"query": "PLACEHOLDER", "limit": 1}\n'
    "Done.</thinking>\n"
    '{"query": "液冷板块资金流向", "limit": 10}'
)


class TestStripReasoningBlocks:
    def test_removes_minimax_think_block(self):
        cleaned = _strip_reasoning_blocks(MINIMAX_WRAPPED)
        assert "<think" not in cleaned
        assert "Let me create the JSON array." not in cleaned
        assert cleaned.lstrip().startswith("[")

    def test_removes_claude_thinking_block(self):
        cleaned = _strip_reasoning_blocks(CLAUDE_WRAPPED)
        assert "<thinking" not in cleaned
        assert "PLACEHOLDER" not in cleaned

    def test_plain_text_is_returned_unchanged(self):
        plain = '[{"content": "no wrapper here"}]'
        assert _strip_reasoning_blocks(plain) == plain

    def test_empty_input(self):
        assert _strip_reasoning_blocks("") == ""


class TestLoadsLlmJsonWithReasoning:
    def test_minimax_wrapped_payload_parses(self):
        result = loads_llm_json(MINIMAX_WRAPPED)
        assert isinstance(result, list)
        assert [item["type"] for item in result] == ["context", "preference"]
        assert "OpenAkita" in result[0]["content"]

    def test_schema_example_inside_reasoning_is_never_returned(self):
        """The actual data-corruption bug: placeholder must not survive."""
        result = loads_llm_json(REASONING_CONTAINS_SCHEMA_EXAMPLE)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["content"] == "USER_LIKES_LIQUID_COOLING"
        assert "EXAMPLE_NOT_REAL" not in json.dumps(result, ensure_ascii=False)

    def test_claude_wrapped_object_parses(self):
        result = loads_llm_json(CLAUDE_WRAPPED)
        assert result == {"query": "液冷板块资金流向", "limit": 10}

    def test_code_fence_after_reasoning_still_supported(self):
        payload = "<think>reasoning here</think>\n```json\n[1, 2, 3]\n```"
        assert loads_llm_json(payload) == [1, 2, 3]

    def test_trailing_comma_repair_still_applies_after_stripping(self):
        payload = '<think>r</think>\n[{"a": 1},]'
        assert loads_llm_json(payload) == [{"a": 1}]

    def test_unclosed_think_block_fails_closed(self):
        """An unterminated ``<think>`` means the answer never arrived.

        Everything after it is still reasoning, so refusing to parse is the
        safe outcome -- better than inventing a memory from truncated thoughts.
        """
        payload = "<think>reasoning that got cut off mid-sentence [{\"type\""
        with pytest.raises(json.JSONDecodeError):
            loads_llm_json(payload)


class TestExtractJsonWithReasoning:
    def test_extract_json_array_skips_reasoning_example(self):
        extracted = extract_json_array(REASONING_CONTAINS_SCHEMA_EXAMPLE)
        assert extracted is not None
        assert "EXAMPLE_NOT_REAL" not in extracted
        assert "USER_LIKES_LIQUID_COOLING" in extracted

    def test_extract_json_object_skips_reasoning_example(self):
        extracted = extract_json_object(CLAUDE_WRAPPED)
        assert extracted == '{"query": "液冷板块资金流向", "limit": 10}'

    def test_extract_json_array_returns_none_when_only_reasoning_matches(self):
        payload = (
            "<think>The schema is "
            '[{"type": "fact", "content": "...", "importance": 3}]</think>\n'
            "I have nothing to report."
        )
        assert extract_json_array(payload) is None

    def test_extract_json_array_on_plain_text_unchanged(self):
        assert extract_json_array('prefix [1, {"a": [2, 3]}] suffix') == '[1, {"a": [2, 3]}]'


class TestCleanTextStillHandlesLegacyInput:
    def test_clean_text_strips_fence_and_reasoning(self):
        payload = "<think>r</think>\n```json\n[1, 2,]\n```"
        cleaned = _clean_llm_json_text(payload)
        assert cleaned == "[1, 2]"


def test_memory_nudge_uses_shared_json_helpers():
    """Guard against re-inlining a fragile regex in the scheduler.

    The old inline parser silently rewrote placeholder text into long-term
    memory. Keeping the nudge on the shared helpers means future reasoning
    formats are handled in exactly one place.
    """
    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "openakita"
        / "scheduler"
        / "executor.py"
    ).read_text(encoding="utf-8")

    assert "from ..memory.json_utils import" in source
    assert "extract_json_array" in source
    assert "loads_llm_json" in source

    # Ignore comments: the fix documents the old regex on purpose.
    code_lines = [
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)

    # The non-greedy "grab the first array" regex that caused the bug must not
    # come back as executable code.
    assert r"(?:\{.*?\}\s*,?\s*)*\]" not in code
    assert "import re as _re" not in code


class TestParseNudgeMemories:
    """The scheduler's nudge-specific wrapper around the shared helpers."""

    @staticmethod
    def _parse(raw: str):
        from openakita.scheduler.executor import _parse_nudge_memories

        return _parse_nudge_memories(raw)

    def test_unfenced_minimax_output(self):
        memories, reason = self._parse(MINIMAX_WRAPPED)
        assert reason == "ok"
        assert [m["type"] for m in memories] == ["context", "preference"]

    def test_fenced_minimax_output(self):
        fenced = (
            "<think>reasoning</think>\n"
            "```json\n"
            '[{"type": "fact", "content": "x", "importance": 3}]\n'
            "```"
        )
        memories, reason = self._parse(fenced)
        assert reason == "ok"
        assert memories == [{"type": "fact", "content": "x", "importance": 3}]

    def test_placeholder_in_reasoning_is_not_persisted(self):
        memories, reason = self._parse(REASONING_CONTAINS_SCHEMA_EXAMPLE)
        assert reason == "ok"
        assert memories == [
            {
                "type": "preference",
                "content": "USER_LIKES_LIQUID_COOLING",
                "importance": 4,
            }
        ]

    def test_plain_array(self):
        memories, reason = self._parse('[{"content": "a", "importance": 3}]')
        assert reason == "ok"
        assert memories == [{"content": "a", "importance": 3}]

    def test_empty_response(self):
        memories, reason = self._parse("   \n  ")
        assert memories is None
        assert reason == "empty response"

    def test_truncated_array_is_rejected_not_invented(self):
        truncated = '<think>r</think>\n[{"type": "fact", "content": "cut off"'
        memories, reason = self._parse(truncated)
        assert memories is None
        assert reason  # a non-empty diagnostic for the warning line

    def test_non_list_json_reports_a_reason(self):
        memories, reason = self._parse('<think>r</think>\n{"error": "no array"}')
        assert memories is None
        assert "non-list" in reason

    def test_object_wrapper_containing_an_array_still_resolves(self):
        wrapped = (
            '<think>r</think>\n{"memories": [{"type": "fact", "content": "a", '
            '"importance": 4}]}'
        )
        memories, reason = self._parse(wrapped)
        assert reason == "ok"
        assert memories == [{"type": "fact", "content": "a", "importance": 4}]
