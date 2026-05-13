from datetime import UTC, datetime, timedelta

from umbrella.memory.recall import summarized_palace_for_prompt


class _FakePalace:
    def __init__(self, hits):
        self._hits = hits

    def search(self, query: str, workspace_id: str, n_results: int):
        return self._hits[:n_results]


def test_recall_filters_low_signal_and_flags_non_gmas() -> None:
    now = datetime.now(UTC).timestamp()
    hits = [
        {
            "content": "success: true",
            "room": "noise",
            "distance": 0.1,
            "metadata": {"timestamp": now},
        },
        {
            "content": "Implemented with FastAPI endpoint and direct requests calls",
            "room": "api",
            "distance": 0.2,
            "metadata": {"timestamp": now},
        },
        {
            "content": "Used gmas RoleGraph and MACPRunner for orchestration",
            "room": "architecture",
            "distance": 0.1,
            "metadata": {
                "timestamp": (datetime.now(UTC) - timedelta(days=2)).timestamp()
            },
        },
    ]
    bundle = summarized_palace_for_prompt(
        palace=_FakePalace(hits),
        query="build multi-agent",
        workspace_id="agent_research",
        token_budget=300,
        require_gmas=True,
    )
    assert bundle.entries
    assert any("RoleGraph" in line for line in bundle.entries)
    assert all("success: true" not in line for line in bundle.entries)
    assert bundle.flagged_non_gmas
