"""Tests for the verify-gate discipline counter.

Covers the soft-warning behavior the user explicitly chose: a system
reminder is generated past the threshold, schemas stay intact, the
reminder is rate-limited, and a successful verify resets the counter.
"""

import unittest
from unittest.mock import patch

from ouroboros.discipline import (
    REMIND_INTERVAL,
    VerifyGate,
    collect_workspace_ids_from_args,
)


class TestVerifyGateCounter(unittest.TestCase):
    def test_writes_below_threshold_do_not_trigger_reminder(self):
        gate = VerifyGate(threshold=10)
        for _ in range(9):
            gate.observe("update_workspace_seed", workspace_id="JKX")
        self.assertFalse(gate.should_remind(round_idx=20))
        self.assertEqual(gate.hot_workspaces(), [])

    def test_writes_at_threshold_trigger_reminder(self):
        gate = VerifyGate(threshold=10)
        for _ in range(10):
            gate.observe("update_workspace_seed", workspace_id="JKX")
        self.assertTrue(gate.should_remind(round_idx=20))
        hot = gate.hot_workspaces()
        self.assertEqual(hot, [("JKX", 10)])

    def test_verify_call_resets_counter_for_workspace(self):
        gate = VerifyGate(threshold=5)
        for _ in range(7):
            gate.observe("update_workspace_seed", workspace_id="JKX")
        self.assertTrue(gate.should_remind(round_idx=10))

        gate.observe("run_workspace_verify", workspace_id="JKX")
        self.assertFalse(gate.should_remind(round_idx=11))
        self.assertEqual(gate.edits.get("JKX", 0), 0)

    def test_per_workspace_isolation(self):
        gate = VerifyGate(threshold=5)
        for _ in range(6):
            gate.observe("update_workspace_seed", workspace_id="JKX")
        for _ in range(2):
            gate.observe("update_workspace_seed", workspace_id="other")
        gate.observe("run_workspace_verify", workspace_id="JKX")
        self.assertEqual(gate.edits["JKX"], 0)
        self.assertEqual(gate.edits["other"], 2)
        self.assertFalse(gate.should_remind(round_idx=10))

    def test_zero_threshold_disables_gate(self):
        gate = VerifyGate(threshold=0)
        for _ in range(100):
            gate.observe("update_workspace_seed", workspace_id="JKX")
        self.assertFalse(gate.should_remind(round_idx=200))
        self.assertEqual(gate.hot_workspaces(), [])

    def test_reminder_is_rate_limited(self):
        gate = VerifyGate(threshold=3)
        for _ in range(4):
            gate.observe("update_workspace_seed", workspace_id="JKX")

        self.assertTrue(gate.should_remind(round_idx=10))
        gate.build_reminder(round_idx=10)
        # Same round + 1 should NOT trigger again — rate limit kicks in.
        self.assertFalse(gate.should_remind(round_idx=10 + 1))
        # After REMIND_INTERVAL it should trigger again.
        self.assertTrue(gate.should_remind(round_idx=10 + REMIND_INTERVAL))

    def test_reminder_first_time_vs_repeat_phrasing(self):
        gate = VerifyGate(threshold=3)
        for _ in range(4):
            gate.observe("update_workspace_seed", workspace_id="JKX")

        first = gate.build_reminder(round_idx=10)
        self.assertIn("Strongly recommended", first["content"])
        self.assertNotIn("second nudge", first["content"])

        # Add more edits, then trigger again.
        for _ in range(3):
            gate.observe("update_workspace_seed", workspace_id="JKX")
        second = gate.build_reminder(round_idx=10 + REMIND_INTERVAL)
        self.assertIn("second nudge", second["content"])

    def test_reminder_mentions_workspace_specific_command(self):
        gate = VerifyGate(threshold=3)
        for _ in range(4):
            gate.observe("commit_workspace_changes", workspace_id="JKX")
        msg = gate.build_reminder(round_idx=5)
        self.assertIn("run_workspace_verify(workspace_id='JKX')", msg["content"])
        self.assertIn("[VERIFY_GATE]", msg["content"])

    def test_non_write_tools_do_not_count(self):
        gate = VerifyGate(threshold=2)
        for _ in range(20):
            gate.observe("read_workspace_file", workspace_id="JKX")
            gate.observe("get_umbrella_memory", workspace_id="JKX")
            gate.observe("list_workspace_files", workspace_id="JKX")
        self.assertFalse(gate.should_remind(round_idx=30))


class TestThresholdResolution(unittest.TestCase):
    def test_default_threshold(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("OUROBOROS_VERIFY_GATE_EDITS", None)
            gate = VerifyGate()
            self.assertEqual(gate.threshold, 50)

    def test_env_var_override(self):
        with patch.dict("os.environ", {"OUROBOROS_VERIFY_GATE_EDITS": "12"}):
            gate = VerifyGate()
            self.assertEqual(gate.threshold, 12)

    def test_invalid_env_falls_back_to_default(self):
        with patch.dict("os.environ", {"OUROBOROS_VERIFY_GATE_EDITS": "not-a-number"}):
            gate = VerifyGate()
            self.assertEqual(gate.threshold, 50)

    def test_negative_env_clamped_to_zero(self):
        with patch.dict("os.environ", {"OUROBOROS_VERIFY_GATE_EDITS": "-5"}):
            gate = VerifyGate()
            self.assertEqual(gate.threshold, 0)


class TestCollectWorkspaceIds(unittest.TestCase):
    def test_extracts_workspace_id_from_dict(self):
        out = collect_workspace_ids_from_args([{"workspace_id": "JKX"}])
        self.assertEqual(out, ["JKX"])

    def test_skips_non_dict_entries(self):
        out = collect_workspace_ids_from_args(["hello", 42, None])
        self.assertEqual(out, [])

    def test_skips_empty_workspace_id(self):
        out = collect_workspace_ids_from_args(
            [{"workspace_id": "   "}, {"workspace_id": ""}]
        )
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
