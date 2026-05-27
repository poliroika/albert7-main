from ouroboros.model_failure import is_model_response_failure


def test_model_empty_response_warning_is_failure() -> None:
    assert is_model_response_failure(
        "Model returned an empty response. Try rephrasing your request."
    )


def test_model_failure_detector_ignores_normal_text() -> None:
    assert not is_model_response_failure(
        "OK: Accepted phase-completion tool(s): submit_micro_review"
    )
