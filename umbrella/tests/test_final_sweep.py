"""Tests for ``umbrella.verification.final_sweep``."""

from pathlib import Path
from textwrap import dedent

from umbrella.verification.final_sweep import (
    SweepSeverity,
    SweepStatus,
    cleanup_nested_workspace_duplicate,
    cleanup_noise_files,
    find_nested_workspace_duplicate,
    parse_required_files,
    run_workspace_sweep,
    scan_noise_files,
)


def test_cleanup_noise_files_removes_debug_and_bak(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('keep')\n", encoding="utf-8")
    (tmp_path / "debug_test.py").write_text("# debug", encoding="utf-8")
    (tmp_path / "scratch_99.py").write_text("# scratch", encoding="utf-8")
    (tmp_path / "settings.toml.bak").write_text("[bak]", encoding="utf-8")
    (tmp_path / "patch.orig").write_text("# orig", encoding="utf-8")

    removed, leftover = cleanup_noise_files(tmp_path, auto_remove=True)

    assert sorted(removed) == sorted(
        ["debug_test.py", "patch.orig", "scratch_99.py", "settings.toml.bak"]
    )
    assert leftover == []
    assert (tmp_path / "main.py").exists()
    assert not (tmp_path / "debug_test.py").exists()


def test_cleanup_noise_files_skips_excluded_dirs(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "debug_internal.py").write_text("# git", encoding="utf-8")
    (tmp_path / ".memory").mkdir()
    (tmp_path / ".memory" / "scratch_pad.py").write_text("# memory", encoding="utf-8")
    (tmp_path / "debug_real.py").write_text("# real", encoding="utf-8")

    removed, _ = cleanup_noise_files(tmp_path, auto_remove=True)

    assert removed == ["debug_real.py"]
    assert (tmp_path / ".git" / "debug_internal.py").exists()
    assert (tmp_path / ".memory" / "scratch_pad.py").exists()


def test_cleanup_noise_files_uses_workspace_path_policy(tmp_path: Path) -> None:
    (tmp_path / "workspace.toml").write_text(
        "[verification]\nskip_paths = ['generated/**']\n[config]\nexclude_paths = ['external/**']\n",
        encoding="utf-8",
    )
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "debug_skip.py").write_text(
        "# generated", encoding="utf-8"
    )
    (tmp_path / "external").mkdir()
    (tmp_path / "external" / "debug_skip.py").write_text("# external", encoding="utf-8")
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "debug_skip.py").write_text("# vendor", encoding="utf-8")
    (tmp_path / "debug_real.py").write_text("# real", encoding="utf-8")

    removed, _ = cleanup_noise_files(tmp_path, auto_remove=True)

    assert removed == ["debug_real.py"]
    assert (tmp_path / "generated" / "debug_skip.py").exists()
    assert (tmp_path / "external" / "debug_skip.py").exists()
    assert (tmp_path / "vendor" / "debug_skip.py").exists()


def test_cleanup_noise_files_dry_run_lists_leftover(tmp_path: Path) -> None:
    (tmp_path / "debug_x.py").write_text("# x", encoding="utf-8")

    removed, leftover = cleanup_noise_files(tmp_path, auto_remove=False)

    assert removed == []
    assert leftover == ["debug_x.py"]
    assert (tmp_path / "debug_x.py").exists()


def test_cleanup_noise_files_removes_no_write_enforced_markers(tmp_path: Path) -> None:
    (tmp_path / "EMPTY_MARKER.txt").write_text("done", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / ".py").write_text(
        "# accidental empty module name", encoding="utf-8"
    )
    (tmp_path / ".verification_status.txt").write_text("ok", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "all_files_verified.txt").write_text("ok", encoding="utf-8")
    (tmp_path / "tests" / "listing_verification.txt").write_text("ok", encoding="utf-8")

    removed, leftover = cleanup_noise_files(tmp_path, auto_remove=True)

    assert sorted(removed) == sorted(
        [
            ".verification_status.txt",
            "EMPTY_MARKER.txt",
            "src/.py",
            "tests/all_files_verified.txt",
            "tests/listing_verification.txt",
        ]
    )
    assert leftover == []


def test_parse_required_files_extracts_project_layout(tmp_path: Path) -> None:
    task_md = tmp_path / "TASK_MAIN.md"
    task_md.write_text(
        dedent(
            """
            # TASK: Demo

            ## Project Layout
            ```
            main.py
            snake/__init__.py
            snake/board.py
            tests/test_board.py
            README.md
            requirements.txt
            ```

            ## Definition of Done
            - tests pass
            """
        ).strip(),
        encoding="utf-8",
    )

    required = parse_required_files(task_md)

    assert "main.py" in required
    assert "snake/board.py" in required
    assert "tests/test_board.py" in required
    assert "README.md" in required
    assert "requirements.txt" in required


def test_parse_required_files_ignores_layout_comments_and_runtime_tokens(
    tmp_path: Path,
) -> None:
    task_md = tmp_path / "TASK_MAIN.md"
    task_md.write_text(
        dedent(
            """
            # TASK: Demo

            ## Project Layout
            ```
            main.py
            snake/score.py  # load_highscore/save_highscore (highscore.txt)
            ui/renderer.py
            README.md
            requirements.txt
            ```
            """
        ).strip(),
        encoding="utf-8",
    )

    required = parse_required_files(task_md)

    assert "main.py" in required
    assert "snake/score.py" in required
    assert "ui/renderer.py" in required
    assert "README.md" in required
    assert "requirements.txt" in required
    assert "highscore.txt" not in required


def test_parse_required_files_falls_back_to_definition_of_done(tmp_path: Path) -> None:
    task_md = tmp_path / "TASK_MAIN.md"
    task_md.write_text(
        dedent(
            """
            # TASK: Demo

            ## Definition of Done
            - `README.md` and `requirements.txt` exist
            - `python main.py` works
            """
        ).strip(),
        encoding="utf-8",
    )

    required = parse_required_files(task_md)

    assert "README.md" in required
    assert "requirements.txt" in required
    assert "main.py" in required


def test_run_workspace_sweep_reports_missing(tmp_path: Path) -> None:
    (tmp_path / "TASK_MAIN.md").write_text(
        dedent(
            """
            # TASK: Demo

            ## Project Layout
            ```
            main.py
            README.md
            requirements.txt
            ```
            """
        ).strip(),
        encoding="utf-8",
    )
    (tmp_path / "main.py").write_text("# present", encoding="utf-8")

    report = run_workspace_sweep(tmp_path)

    assert "main.py" in report.expected_files
    assert "README.md" in report.missing_required
    assert "requirements.txt" in report.missing_required
    assert report.passed is False


def test_run_workspace_sweep_passes_when_clean(tmp_path: Path) -> None:
    (tmp_path / "TASK_MAIN.md").write_text(
        dedent(
            """
            # TASK: Demo

            ## Project Layout
            ```
            main.py
            ```
            """
        ).strip(),
        encoding="utf-8",
    )
    (tmp_path / "main.py").write_text("# ok", encoding="utf-8")

    report = run_workspace_sweep(tmp_path)

    assert report.missing_required == []
    assert report.removed == []
    assert report.passed is True


def test_cleanup_noise_removes_get_pip_and_subtask_artifacts(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('keep')\n", encoding="utf-8")
    (tmp_path / "get-pip.py").write_text("# bootstrap pip", encoding="utf-8")
    (tmp_path / "subtask1_installed_dependencies.txt").write_text(
        "ok", encoding="utf-8"
    )
    (tmp_path / "uvicorn_installed_dependencies.txt").write_text("ok", encoding="utf-8")

    removed, leftover = cleanup_noise_files(tmp_path, auto_remove=True)

    assert "get-pip.py" in removed
    assert "subtask1_installed_dependencies.txt" in removed
    assert "uvicorn_installed_dependencies.txt" in removed
    assert leftover == []
    assert (tmp_path / "main.py").exists()


def test_cleanup_noise_removes_root_diagnostics_but_keeps_src_helpers(
    tmp_path: Path,
) -> None:
    (tmp_path / "check_layout.py").write_text("# diagnostic", encoding="utf-8")
    (tmp_path / "inspect_output.py").write_text("# diagnostic", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "check_layout.py").write_text("# real helper", encoding="utf-8")

    removed, leftover = cleanup_noise_files(tmp_path, auto_remove=True)

    assert sorted(removed) == ["check_layout.py", "inspect_output.py"]
    assert leftover == []
    assert (tmp_path / "src" / "check_layout.py").exists()


def test_find_nested_workspace_duplicate_detects_self_nesting(tmp_path: Path) -> None:
    workspace = tmp_path / "demo_ws"
    workspace.mkdir()
    nested = workspace / "workspaces" / "demo_ws"
    nested.mkdir(parents=True)
    (nested / "main.py").write_text("# nested", encoding="utf-8")

    duplicate = find_nested_workspace_duplicate(workspace)

    assert duplicate is not None
    assert duplicate.resolve() == nested.resolve()


def test_cleanup_nested_workspace_duplicate_removes_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "demo_ws"
    workspace.mkdir()
    nested = workspace / "workspaces" / "demo_ws" / "weather_ui"
    nested.mkdir(parents=True)
    (nested / "data_provider.py").write_text("x", encoding="utf-8")

    rel = cleanup_nested_workspace_duplicate(workspace, auto_remove=True)

    assert rel == "workspaces/demo_ws"
    assert not (workspace / "workspaces").exists()


def test_cleanup_nested_workspace_duplicate_dry_run_keeps_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "demo_ws"
    workspace.mkdir()
    nested = workspace / "workspaces" / "demo_ws"
    nested.mkdir(parents=True)
    (nested / "main.py").write_text("x", encoding="utf-8")

    rel = cleanup_nested_workspace_duplicate(workspace, auto_remove=False)

    assert rel == "workspaces/demo_ws"
    assert nested.exists()


def test_scan_noise_flags_root_diagnostic_scripts_as_block(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("# real", encoding="utf-8")
    (tmp_path / "debug_docx.py").write_text("# debug", encoding="utf-8")
    (tmp_path / "extract_docx.py").write_text("# debug extractor", encoding="utf-8")
    (tmp_path / "fix_profile_func.py").write_text("# debug fix", encoding="utf-8")
    (tmp_path / "probe_input.py").write_text("# probe", encoding="utf-8")
    (tmp_path / "real_test_output.pptx").write_text("artifact", encoding="utf-8")

    removed, leftover, blocking, warning = scan_noise_files(tmp_path, auto_remove=False)

    block_paths = {h.path for h in blocking}
    assert "debug_docx.py" in block_paths
    assert "extract_docx.py" in block_paths
    assert "fix_profile_func.py" in block_paths
    assert "probe_input.py" in block_paths
    assert "real_test_output.pptx" in block_paths
    # main.py is not noise:
    assert "main.py" not in block_paths
    assert all(h.severity == SweepSeverity.BLOCK for h in blocking)


def test_scan_noise_classifies_handoff_docs_as_block_when_docs_dir_exists(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "main.py").write_text("# ok", encoding="utf-8")
    (tmp_path / "handoff_v2.md").write_text("# handoff", encoding="utf-8")
    (tmp_path / "agent_topology_diagram.md").write_text("# topology", encoding="utf-8")

    _removed, _leftover, blocking, _warning = scan_noise_files(
        tmp_path, auto_remove=False
    )

    block_paths = {h.path for h in blocking}
    assert "handoff_v2.md" in block_paths
    assert "agent_topology_diagram.md" in block_paths
    assert all(h.category == "noise.docs" for h in blocking)


def test_scan_noise_demotes_handoff_docs_when_no_docs_dir(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("# ok", encoding="utf-8")
    (tmp_path / "handoff_v2.md").write_text("# handoff", encoding="utf-8")

    _removed, _leftover, blocking, warning = scan_noise_files(
        tmp_path, auto_remove=False
    )

    assert all(h.path != "handoff_v2.md" for h in blocking)
    assert any(h.path == "handoff_v2.md" for h in warning)


def test_run_workspace_sweep_status_failed_on_block_level_noise(tmp_path: Path) -> None:
    (tmp_path / "TASK_MAIN.md").write_text(
        dedent(
            """
            # TASK: Demo

            ## Project Layout
            ```
            main.py
            ```
            """
        ).strip(),
        encoding="utf-8",
    )
    (tmp_path / "main.py").write_text("# ok", encoding="utf-8")
    (tmp_path / "extract_docx.py").write_text("# ad-hoc", encoding="utf-8")
    (tmp_path / "real_test_output.pptx").write_text("blob", encoding="utf-8")

    report = run_workspace_sweep(tmp_path)

    assert report.status == SweepStatus.FAILED
    assert any(h.path == "extract_docx.py" for h in report.blocking_noise)
    assert any(h.path == "real_test_output.pptx" for h in report.blocking_noise)
    assert report.removed == []
    assert (tmp_path / "extract_docx.py").exists()
    assert "BLOCKING noise" in report.render_summary()
    # to_dict round-trip exposes the new fields.
    payload = report.to_dict()
    assert payload["status"] == "failed"
    assert any(
        item["category"].startswith("noise.") for item in payload["blocking_noise"]
    )


def test_run_workspace_sweep_status_warning_on_warn_only_noise(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("# ok", encoding="utf-8")
    (tmp_path / "config.toml.bak").write_text("backup", encoding="utf-8")

    report = run_workspace_sweep(tmp_path, auto_clean=False)

    # bak is warn-level, not block.
    assert report.blocking_noise == []
    assert any(h.path == "config.toml.bak" for h in report.warning_noise)
    assert report.status == SweepStatus.WARNING


def test_run_workspace_sweep_records_nested_duplicate(tmp_path: Path) -> None:
    workspace = tmp_path / "demo_ws"
    workspace.mkdir()
    (workspace / "TASK_MAIN.md").write_text(
        dedent(
            """
            # TASK: Demo

            ## Project Layout
            ```
            main.py
            ```
            """
        ).strip(),
        encoding="utf-8",
    )
    (workspace / "main.py").write_text("# ok", encoding="utf-8")
    nested = workspace / "workspaces" / "demo_ws"
    nested.mkdir(parents=True)
    (nested / "main.py").write_text("# nested copy", encoding="utf-8")

    report = run_workspace_sweep(workspace)

    assert "workspaces/demo_ws" in report.leftover_noise
    assert any(h.path == "workspaces/demo_ws" for h in report.blocking_noise)
    assert report.missing_required == []
    assert (workspace / "workspaces").exists()


def test_final_sweep_flags_result_txt_pycache_and_src_diagnostics(
    tmp_path: Path,
) -> None:
    (tmp_path / "src" / "demo").mkdir(parents=True)
    (tmp_path / "src" / "demo" / "app.py").write_text("# app", encoding="utf-8")
    (tmp_path / "src" / "demo" / "analyze_spec.py").write_text(
        "# probe", encoding="utf-8"
    )
    (tmp_path / "src" / "demo" / "__pycache__").mkdir()
    (tmp_path / "src" / "demo" / "__pycache__" / "app.cpython-312.pyc").write_bytes(
        b"\0"
    )
    (tmp_path / "result.txt").write_text("debug output", encoding="utf-8")

    report = run_workspace_sweep(tmp_path, auto_clean=False)

    block_paths = {h.path for h in report.blocking_noise}
    assert "result.txt" in block_paths
    assert "src/demo/analyze_spec.py" in block_paths
    assert "src/demo/__pycache__/app.cpython-312.pyc" not in block_paths
    assert report.status == SweepStatus.FAILED
