from umbrella.deep_agent_tools.memory import memory_write_policy_issues


def test_durable_memory_requires_evidence_refs() -> None:
    issues = memory_write_policy_issues(
        kind="architecture_decision",
        tags=["durable"],
        metadata={"scope": "cross_run_durable"},
    )

    assert issues
    assert "trust_level" in issues[0]


def test_observation_memory_can_be_written_without_evidence_refs() -> None:
    assert (
        memory_write_policy_issues(
            kind="observation",
            tags=["progress"],
            metadata={"scope": "working"},
        )
        == []
    )


def test_durable_memory_requires_typed_verified_evidence_ref() -> None:
    issues = memory_write_policy_issues(
        kind="architecture_decision",
        tags=["durable"],
        metadata={"trust_level": "public_verified", "verify_run_id": "verify_123"},
    )

    assert issues
    assert "typed EvidenceRef" in issues[0]


def test_durable_memory_allows_typed_verified_evidence_ref() -> None:
    assert (
        memory_write_policy_issues(
            kind="architecture_decision",
            tags=["durable"],
            metadata={
                "trust_level": "public_verified",
                "evidence_refs": [
                    {
                        "ref_type": "verification_report",
                        "ref_id": "verify_123",
                        "produced_by": "verifier",
                    }
                ],
            },
        )
        == []
    )
