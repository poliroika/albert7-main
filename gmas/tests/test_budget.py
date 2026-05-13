"""Comprehensive tests for src/execution/budget.py"""

from gmas.execution.budget import Budget, BudgetConfig, BudgetTracker, NodeBudget

# ─────────────────────────── Budget ───────────────────────────────────────────


class TestBudget:
    def test_initial_state(self):
        b = Budget(limit=100.0)
        assert b.limit == 100.0
        assert b.used == 0.0
        assert b.reserved == 0.0

    def test_available_no_usage(self):
        b = Budget(limit=100.0)
        assert b.available == 100.0

    def test_available_after_spend(self):
        b = Budget(limit=100.0)
        b.spend(40.0)
        assert b.available == 60.0

    def test_available_with_reservation(self):
        b = Budget(limit=100.0)
        b.reserve(30.0)
        assert b.available == 70.0

    def test_available_combined(self):
        b = Budget(limit=100.0)
        b.spend(30.0)
        b.reserve(20.0)
        assert b.available == 50.0

    def test_remaining(self):
        b = Budget(limit=100.0)
        b.spend(40.0)
        assert b.remaining == 60.0

    def test_remaining_ignores_reservation(self):
        b = Budget(limit=100.0)
        b.spend(20.0)
        b.reserve(30.0)
        # remaining = limit - used (ignores reserved)
        assert b.remaining == 80.0

    def test_usage_ratio(self):
        b = Budget(limit=100.0)
        b.spend(50.0)
        assert b.usage_ratio == 0.5

    def test_usage_ratio_zero_limit(self):
        b = Budget(limit=0.0)
        assert b.usage_ratio == 0.0

    def test_is_exhausted_false(self):
        b = Budget(limit=100.0)
        b.spend(50.0)
        assert b.is_exhausted is False

    def test_is_exhausted_true(self):
        b = Budget(limit=100.0)
        b.spend(100.0)
        assert b.is_exhausted is True

    def test_is_exhausted_reserved_counts(self):
        b = Budget(limit=100.0)
        b.spend(80.0)
        b.reserve(20.0)
        assert b.is_exhausted is True

    def test_can_spend_yes(self):
        b = Budget(limit=100.0)
        assert b.can_spend(50.0) is True

    def test_can_spend_no(self):
        b = Budget(limit=100.0)
        b.spend(60.0)
        assert b.can_spend(50.0) is False

    def test_can_spend_exact(self):
        b = Budget(limit=100.0)
        assert b.can_spend(100.0) is True

    def test_spend_success(self):
        b = Budget(limit=100.0)
        result = b.spend(50.0)
        assert result is True
        assert b.used == 50.0

    def test_spend_failure(self):
        b = Budget(limit=50.0)
        b.spend(40.0)
        result = b.spend(20.0)
        assert result is False
        assert b.used == 40.0  # unchanged

    def test_reserve_success(self):
        b = Budget(limit=100.0)
        result = b.reserve(30.0)
        assert result is True
        assert b.reserved == 30.0

    def test_reserve_failure_insufficient(self):
        b = Budget(limit=50.0)
        b.spend(30.0)
        result = b.reserve(30.0)
        assert result is False
        assert b.reserved == 0.0

    def test_release_reservation(self):
        b = Budget(limit=100.0)
        b.reserve(40.0)
        b.release_reservation(20.0)
        assert b.reserved == 20.0

    def test_release_reservation_below_zero(self):
        b = Budget(limit=100.0)
        b.reserve(10.0)
        b.release_reservation(50.0)  # can't go below 0
        assert b.reserved == 0.0

    def test_commit_reservation(self):
        b = Budget(limit=100.0)
        b.reserve(30.0)
        b.commit_reservation(30.0)
        assert b.reserved == 0.0
        assert b.used == 30.0

    def test_commit_reservation_partial(self):
        b = Budget(limit=100.0)
        b.reserve(40.0)
        b.commit_reservation(20.0)
        assert b.reserved == 20.0
        assert b.used == 20.0

    def test_reset(self):
        b = Budget(limit=100.0)
        b.spend(50.0)
        b.reserve(20.0)
        b.reset()
        assert b.used == 0.0
        assert b.reserved == 0.0

    def test_to_dict(self):
        b = Budget(limit=100.0)
        b.spend(40.0)
        d = b.to_dict()
        assert d["limit"] == 100.0
        assert d["used"] == 40.0
        assert d["available"] == 60.0
        assert "usage_ratio" in d

    def test_available_never_negative(self):
        b = Budget(limit=100.0)
        # Force overuse scenario
        b.used = 110.0
        assert b.available == 0.0

    def test_remaining_never_negative(self):
        b = Budget(limit=100.0)
        b.used = 110.0
        assert b.remaining == 0.0


# ─────────────────────────── NodeBudget ───────────────────────────────────────


class TestNodeBudget:
    def test_can_execute_no_limits(self):
        nb = NodeBudget(node_id="agent1")
        can, reason = nb.can_execute()
        assert can is True
        assert reason is None

    def test_can_execute_token_limit_ok(self):
        nb = NodeBudget(node_id="agent1", tokens=Budget(limit=1000.0))
        can, _reason = nb.can_execute(estimated_tokens=100)
        assert can is True

    def test_can_execute_token_limit_exceeded(self):
        nb = NodeBudget(node_id="agent1", tokens=Budget(limit=50.0, used=40.0))
        can, reason = nb.can_execute(estimated_tokens=20)
        assert can is False
        assert reason
        assert "Token budget exhausted" in reason

    def test_can_execute_request_limit_exceeded(self):
        nb = NodeBudget(node_id="agent1", requests=Budget(limit=2.0, used=2.0))
        can, reason = nb.can_execute()
        assert can is False
        assert reason
        assert "Request budget exhausted" in reason

    def test_record_usage_tokens(self):
        nb = NodeBudget(node_id="agent1", tokens=Budget(limit=1000.0))
        nb.record_usage(tokens=100)
        assert nb.tokens is not None
        assert nb.tokens.used == 100.0

    def test_record_usage_requests(self):
        nb = NodeBudget(node_id="agent1", requests=Budget(limit=10.0))
        nb.record_usage()
        assert nb.requests is not None
        assert nb.requests.used == 1.0

    def test_record_usage_time(self):
        nb = NodeBudget(node_id="agent1", time_seconds=Budget(limit=60.0))
        nb.record_usage(time_seconds=5.0)
        assert nb.time_seconds is not None
        assert nb.time_seconds.used == 5.0

    def test_to_dict(self):
        nb = NodeBudget(
            node_id="agent1",
            tokens=Budget(limit=1000.0),
            max_prompt_length=512,
        )
        d = nb.to_dict()
        assert d["node_id"] == "agent1"
        assert d["tokens"] is not None
        assert d["limits"]["max_prompt_length"] == 512

    def test_to_dict_no_limits(self):
        nb = NodeBudget(node_id="agent1")
        d = nb.to_dict()
        assert d["tokens"] is None
        assert d["requests"] is None


# ─────────────────────────── BudgetTracker ────────────────────────────────────


class TestBudgetTrackerInit:
    def test_default_init(self):
        tracker = BudgetTracker()
        assert tracker.config is not None
        assert tracker._global_tokens.limit == float("inf")

    def test_with_config(self):
        config = BudgetConfig(
            total_token_limit=10000,
            total_request_limit=100,
        )
        tracker = BudgetTracker(config=config)
        assert tracker._global_tokens.limit == 10000.0
        assert tracker._global_requests.limit == 100.0


class TestBudgetTrackerExecution:
    def setup_method(self):
        self.config = BudgetConfig(
            total_token_limit=1000,
            total_request_limit=10,
            node_token_limit=500,
            node_request_limit=5,
        )
        self.tracker = BudgetTracker(config=self.config)

    def test_can_execute_initially(self):
        can, reason = self.tracker.can_execute("agent1", estimated_tokens=100)
        assert can is True
        assert reason is None

    def test_can_execute_after_exhausting_global_tokens(self):
        # Spend in increments to exhaust the 1000-token global limit
        for _ in range(5):
            self.tracker.record_usage("agent1", prompt_tokens=200, completion_tokens=0)
        can, reason = self.tracker.can_execute("agent2", estimated_tokens=1)
        assert can is False
        assert reason is not None
        assert "token" in reason.lower()

    def test_can_execute_after_exhausting_global_requests(self):
        for i in range(10):
            self.tracker.record_usage(f"agent{i}", prompt_tokens=1, completion_tokens=0)
        can, reason = self.tracker.can_execute("agent_new")
        assert can is False
        assert reason is not None
        assert "request" in reason.lower()

    def test_record_usage_updates_global(self):
        self.tracker.record_usage("agent1", prompt_tokens=100, completion_tokens=50)
        assert self.tracker.global_tokens.used == 150.0
        assert self.tracker.global_requests.used == 1.0

    def test_record_usage_creates_node_budget(self):
        self.tracker.record_usage("agent1", prompt_tokens=50, completion_tokens=50)
        nb = self.tracker.get_node_budget("agent1")
        assert nb.node_id == "agent1"

    def test_get_node_budget_creates_if_not_exists(self):
        nb = self.tracker.get_node_budget("new_agent")
        assert nb.node_id == "new_agent"

    def test_can_execute_node_token_limit(self):
        # Use up node tokens (node_token_limit=500 in config)
        self.tracker.record_usage("agent1", prompt_tokens=250, completion_tokens=250)
        can, _reason = self.tracker.can_execute("agent1", estimated_tokens=1)
        assert can is False

    def test_truncate_prompt_short(self):
        config = BudgetConfig(max_prompt_length=100)
        tracker = BudgetTracker(config=config)
        prompt = "Hello world"
        assert tracker.truncate_prompt(prompt) == prompt

    def test_truncate_prompt_long(self):
        config = BudgetConfig(max_prompt_length=10)
        tracker = BudgetTracker(config=config)
        prompt = "Hello world"
        result = tracker.truncate_prompt(prompt)
        assert result.endswith("[TRUNCATED]")
        assert len(result) > 10

    def test_truncate_response_short(self):
        config = BudgetConfig(max_response_length=100)
        tracker = BudgetTracker(config=config)
        response = "Short response"
        assert tracker.truncate_response(response) == response

    def test_truncate_response_long(self):
        config = BudgetConfig(max_response_length=5)
        tracker = BudgetTracker(config=config)
        response = "Long response text"
        result = tracker.truncate_response(response)
        assert "[TRUNCATED]" in result

    def test_reset(self):
        self.tracker.record_usage("agent1", prompt_tokens=100, completion_tokens=50)
        self.tracker.reset()
        assert self.tracker.global_tokens.used == 0.0
        assert self.tracker.global_requests.used == 0.0
        node_budget = self.tracker.get_node_budget("agent1")
        assert node_budget is not None
        assert node_budget.tokens is not None
        assert node_budget.tokens.used == 0.0

    def test_get_summary(self):
        self.tracker.record_usage("agent1", prompt_tokens=100, completion_tokens=50)
        summary = self.tracker.get_summary()
        assert "global" in summary
        assert "nodes" in summary
        assert "agent1" in summary["nodes"]

    def test_get_elapsed_before_start(self):
        assert self.tracker.get_elapsed_seconds() == 0.0

    def test_start_and_elapsed(self):
        self.tracker.start()
        elapsed = self.tracker.get_elapsed_seconds()
        assert elapsed >= 0.0


class TestBudgetTrackerTimeLimits:
    def test_time_limit_not_exceeded(self):
        config = BudgetConfig(total_time_limit_seconds=60.0)
        tracker = BudgetTracker(config=config)
        tracker.start()
        can, _reason = tracker.can_execute("agent1")
        assert can is True

    def test_global_time_property(self):
        config = BudgetConfig(total_time_limit_seconds=30.0)
        tracker = BudgetTracker(config=config)
        assert tracker.global_time.limit == 30.0


class TestBudgetTrackerTimeLimitExhausted:
    def test_can_execute_returns_false_when_time_exhausted(self):
        """Lines 211-214: can_execute returns False when time budget is exhausted."""
        from unittest.mock import patch

        config = BudgetConfig(total_time_limit_seconds=1.0)
        tracker = BudgetTracker(config=config)
        tracker.start()
        # Manually exhaust the time budget
        tracker._global_time.spend(1.0)  # Spend the full limit
        # Mock elapsed to be >= limit
        with patch.object(tracker, "get_elapsed_seconds", return_value=2.0):
            can, reason = tracker.can_execute("agent1")
        # Should return False due to time limit
        assert can is False
        assert reason is not None
        assert "time" in reason.lower() or "budget" in reason.lower()


class TestBudgetTrackerWarnings:
    def test_warning_callback_fired(self):
        warnings = []

        def warn_callback(name, budget):
            warnings.append(name)

        config = BudgetConfig(
            total_token_limit=100,
            warn_at_usage_ratio=0.8,
            on_budget_warning=warn_callback,
        )
        tracker = BudgetTracker(config=config)
        tracker.record_usage("agent1", prompt_tokens=85, completion_tokens=0)
        assert "tokens" in warnings

    def test_no_warning_below_threshold(self):
        warnings = []

        def warn_callback(name, budget):
            warnings.append(name)

        config = BudgetConfig(
            total_token_limit=100,
            warn_at_usage_ratio=0.9,
            on_budget_warning=warn_callback,
        )
        tracker = BudgetTracker(config=config)
        tracker.record_usage("agent1", prompt_tokens=50, completion_tokens=0)
        assert "tokens" not in warnings

    def test_warning_callback_fired_for_requests(self):
        """Line 271: on_budget_warning fires for requests when threshold exceeded."""
        warnings = []

        def warn_callback(name, budget):
            warnings.append(name)

        config = BudgetConfig(
            total_request_limit=5,
            warn_at_usage_ratio=0.8,
            on_budget_warning=warn_callback,
        )
        tracker = BudgetTracker(config=config)
        # Make 5 requests out of limit=5 → usage_ratio = 1.0 >= 0.8
        for i in range(5):
            tracker.record_usage(f"agent{i}", prompt_tokens=1, completion_tokens=0)
        assert "requests" in warnings

    def test_no_truncation_without_config(self):
        tracker = BudgetTracker()
        prompt = "x" * 1000
        assert tracker.truncate_prompt(prompt) == prompt
        assert tracker.truncate_response(prompt) == prompt
