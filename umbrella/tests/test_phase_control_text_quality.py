"""Script-aware handoff text quality checks."""

from umbrella.deep_agent_tools.phase_control_text_quality import (
    _looks_like_mojibake,
)
from umbrella.deep_agent_tools.phase_control_text_quality import (
    _HANDOFF_PLACEHOLDER_RE,
)


def test_mojibake_rejects_latin1_corruption():
    notes = (
        "\u00e7\u00a0\u201d\u00e7\u00a9\u00b6\u00e5\u00ae\u0152"
        "\u00e6\u02c6\u0090\u00e3\u20ac\u201a FastAPI evidence."
    )
    assert _looks_like_mojibake(notes)


def test_mojibake_accepts_legitimate_chinese_handoff():
    notes = (
        "本阶段研究了文明游戏的 Web 桥接层：FastAPI 路由、静态资源挂载、"
        "以及任务主循环与 drive 日志的对接方式，并记录了可复现的验证步骤。"
    )
    assert not _looks_like_mojibake(notes)


def test_placeholder_detects_chinese_pending_phrases():
    assert _HANDOFF_PLACEHOLDER_RE.search("研究进行中，稍后补充具体结论。")
