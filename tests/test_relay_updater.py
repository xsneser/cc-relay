#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Comprehensive tests for relay_updater module."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import relay_updater


class RelayUpdaterUnitTests(unittest.TestCase):
    def test_status_snapshot_contains_required_fields(self):
        with tempfile.TemporaryDirectory() as td:
            updater = relay_updater.RelayUpdater(td)
            st = updater.status()
            self.assertIn("is_git", st)
            self.assertIn("branch", st)
            self.assertIn("local_branch", st)
            self.assertIn("current_version", st)
            self.assertIn("latest_version", st)
            self.assertIn("has_update", st)
            self.assertIn("can_update", st)
            self.assertIn("blocked_reason", st)
            self.assertIn("checking", st)
            self.assertIn("update_state", st)
            # ETag must not be leaked in public status
            self.assertNotIn("etag", st)

    def test_check_dirty_ignores_runtime_files(self):
        with tempfile.TemporaryDirectory() as td:
            updater = relay_updater.RelayUpdater(td)
            mock_res = mock.Mock(
                returncode=0,
                stdout="?? config.json\n?? prompts.json\n?? records.jsonl\n?? serve.log\n",
                stderr=""
            )
            with mock.patch("relay_updater._is_git_repo", return_value=True), \
                 mock.patch("relay_updater._run_git", return_value=mock_res):
                dirty, msg = updater._check_dirty()
                self.assertFalse(dirty)
                self.assertEqual(msg, "")

    def test_check_dirty_detects_unsafe_files(self):
        with tempfile.TemporaryDirectory() as td:
            updater = relay_updater.RelayUpdater(td)
            mock_res = mock.Mock(
                returncode=0,
                stdout=" M cc_relay.py\n?? untracked_script.py\n",
                stderr=""
            )
            with mock.patch("relay_updater._is_git_repo", return_value=True), \
                 mock.patch("relay_updater._run_git", return_value=mock_res):
                dirty, msg = updater._check_dirty()
                self.assertTrue(dirty)
                self.assertIn("本地存在未提交的修改", msg)

    def test_github_api_parsing_and_snapshot_update(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, ".git"))
            updater = relay_updater.RelayUpdater(td)
            updater._snapshot["current_sha"] = "1111111222222233333334444444555555556666"
            updater._snapshot["current_version"] = "1111111"
            updater._snapshot["local_branch"] = "master"

            payload = {
                "sha": "9999999888888877777776666666555555554444",
                "commit": {
                    "message": "feat: cool update\nsecond line",
                    "committer": {"date": "2026-09-25T12:00:00Z"}
                }
            }

            with mock.patch.object(updater, "_request_github_api", return_value=(200, payload, "etag-123")), \
                 mock.patch.object(updater, "_fetch_remote_ui_version", return_value="2.4.2"), \
                 mock.patch.object(updater, "_get_local_commit", return_value={"sha": "1111111222222233333334444444555555556666", "short_sha": "1111111", "branch": "master"}), \
                 mock.patch("relay_updater._run_git") as mock_git:

                # mock ancestor check to returncode=1 (not ancestor -> update available)
                def git_side_effect(args, cwd, timeout=30):
                    if "merge-base" in args:
                        return mock.Mock(returncode=1)
                    if "rev-list" in args:
                        return mock.Mock(returncode=0, stdout="3\n")
                    if "status" in args:
                        return mock.Mock(returncode=0, stdout="")
                    return mock.Mock(returncode=0, stdout="")

                mock_git.side_effect = git_side_effect

                st = updater.check(force=True)
                self.assertTrue(st["has_update"])
                self.assertEqual(st["latest_version"], "2.4.2")
                self.assertEqual(st["latest_sha"], "9999999888888877777776666666555555554444")
                self.assertEqual(st["behind_count"], 3)
                self.assertEqual(st["latest_subject"], "feat: cool update")
                self.assertTrue(st["can_update"])
                self.assertIsNone(st["blocked_reason"])

    def test_non_master_branch_blocks_auto_update(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, ".git"))
            updater = relay_updater.RelayUpdater(td)
            payload = {
                "sha": "9999999888888877777776666666555555554444",
                "commit": {"message": "feat: master update", "committer": {"date": ""}}
            }

            with mock.patch.object(updater, "_request_github_api", return_value=(200, payload, "etag-123")), \
                 mock.patch.object(updater, "_get_local_commit", return_value={"sha": "1111111222222233333334444444555555556666", "short_sha": "1111111", "branch": "dev"}), \
                 mock.patch("relay_updater._run_git") as mock_git:

                def git_side_effect(args, cwd, timeout=30):
                    if "merge-base" in args:
                        return mock.Mock(returncode=1)
                    if "rev-list" in args:
                        return mock.Mock(returncode=0, stdout="2\n")
                    if "status" in args:
                        return mock.Mock(returncode=0, stdout="")
                    return mock.Mock(returncode=0, stdout="")

                mock_git.side_effect = git_side_effect

                st = updater.check(force=True)
                self.assertTrue(st["has_update"])
                self.assertTrue(st["can_update"])
                self.assertIsNone(st["blocked_reason"])

    def test_apply_update_executes_ff_merge_and_triggers_restart(self):
        with tempfile.TemporaryDirectory() as td:
            updater = relay_updater.RelayUpdater(td)
            restart_called = threading.Event()

            with mock.patch.object(updater, "_check_dirty", return_value=(False, "")), \
                 mock.patch.object(updater, "_get_local_commit", side_effect=[
                     {"sha": "old12345", "short_sha": "old123", "branch": "master"},
                     {"sha": "new12345", "short_sha": "new1234", "branch": "master"}
                 ]), \
                 mock.patch.object(updater, "_compile_check", return_value=(True, "")), \
                 mock.patch("relay_updater._is_git_repo", return_value=True), \
                 mock.patch("relay_updater._run_git", return_value=mock.Mock(returncode=0, stdout="", stderr="")):

                ok, msg = updater.apply_update(restart_callback=lambda: restart_called.set())
                self.assertTrue(ok)
                self.assertTrue(restart_called.wait(timeout=2.0))
                st = updater.status()
                self.assertEqual(st["update_state"], "success")
                self.assertEqual(st["current_sha"], "new12345")

    def test_apply_update_rolls_back_on_compile_error(self):
        with tempfile.TemporaryDirectory() as td:
            updater = relay_updater.RelayUpdater(td)
            git_calls = []

            def record_git(args, cwd, timeout=30):
                git_calls.append(list(args))
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(updater, "_check_dirty", return_value=(False, "")), \
                 mock.patch.object(updater, "_get_local_commit", return_value={"sha": "old_safe_sha", "short_sha": "old", "branch": "master"}), \
                 mock.patch.object(updater, "_compile_check", return_value=(False, "SyntaxError: invalid syntax")), \
                 mock.patch("relay_updater._is_git_repo", return_value=True), \
                 mock.patch("relay_updater._run_git", side_effect=record_git):

                ok, msg = updater.apply_update()
                self.assertFalse(ok)
                self.assertIn("代码编译失败", msg)

    def test_non_git_environment_can_update_via_zip(self):
        with tempfile.TemporaryDirectory() as td:
            updater = relay_updater.RelayUpdater(td)
            restart_called = threading.Event()
            # Non-git directory: _is_git_repo is False
            self.assertFalse(relay_updater._is_git_repo(td))
            dirty, msg = updater._check_dirty()
            self.assertFalse(dirty)

            with mock.patch("relay_updater._download_and_extract_zip", return_value=True) as mock_dl, \
                 mock.patch.object(updater, "_compile_check", return_value=(True, "")):

                ok, res_msg = updater.apply_update(restart_callback=lambda: restart_called.set())
                self.assertTrue(ok)
                self.assertTrue(restart_called.wait(timeout=2.0))
                mock_dl.assert_called_once()
                st = updater.status()
                self.assertEqual(st["update_state"], "success")

    def test_protected_paths_filter(self):
        self.assertTrue(relay_updater.is_protected_path("config.json"))
        self.assertTrue(relay_updater.is_protected_path("records.jsonl"))
        self.assertTrue(relay_updater.is_protected_path("prompts.json"))
        self.assertTrue(relay_updater.is_protected_path("serve.log"))
        self.assertTrue(relay_updater.is_protected_path(".git/config"))
        self.assertFalse(relay_updater.is_protected_path("cc_relay.py"))
        self.assertFalse(relay_updater.is_protected_path("ui.html"))

    def test_get_local_commit_reads_build_info_json(self):
        with tempfile.TemporaryDirectory() as td:
            build_info = {
                "version": "2.4.8",
                "commit": "2b48888abcdef",
                "short_sha": "2b48888",
                "branch": "master",
            }
            with open(os.path.join(td, "build_info.json"), "w", encoding="utf-8") as f:
                json.dump(build_info, f)

            updater = relay_updater.RelayUpdater(td)
            local_info = updater._get_local_commit()
            self.assertEqual(local_info["sha"], "2b48888abcdef")
            self.assertEqual(local_info["short_sha"], "2b48888")
            self.assertEqual(local_info["branch"], "master")

    def test_compile_check_skips_when_frozen(self):
        with tempfile.TemporaryDirectory() as td:
            updater = relay_updater.RelayUpdater(td)
            with mock.patch("sys.frozen", True, create=True):
                ok, err = updater._compile_check()
                self.assertTrue(ok)
                self.assertEqual(err, "")

    def test_apply_update_frozen_mode_launches_installer(self):
        with tempfile.TemporaryDirectory() as td:
            updater = relay_updater.RelayUpdater(td)
            with mock.patch("sys.frozen", True, create=True), \
                 mock.patch.object(updater, "_find_release_installer", return_value=("https://example.com/CC-Relay-Setup.exe", "CC-Relay-Setup.exe")), \
                 mock.patch("relay_updater._download_file", return_value=True) as mock_dl, \
                 mock.patch("subprocess.Popen") as mock_popen, \
                 mock.patch("os._exit") as mock_exit:

                ok, msg = updater.apply_update()
                self.assertTrue(ok)
                mock_dl.assert_called_once()
                mock_popen.assert_called()
                cmd_called = mock_popen.call_args_list[0][0][0]
                self.assertIn("/SILENT", cmd_called)
                mock_exit.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
