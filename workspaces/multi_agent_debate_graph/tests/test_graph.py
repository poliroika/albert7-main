from multi_agent_debate_graph.graph import build_debate_skeleton


def test_build_debate_skeleton_has_expected_agents() -> None:
    graph = build_debate_skeleton()
    ids = graph.role_sequence
    assert "framer" in ids
    assert "advocate" in ids
    assert "skeptic" in ids
    assert "synthesizer" in ids
    assert graph.num_nodes >= 4
