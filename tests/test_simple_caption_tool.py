#!/usr/bin/env python3
"""Platform-independent unit tests for simple_caption_tool pure logic."""

from __future__ import annotations

import math
import unittest

import simple_caption_tool as app


class NormalizeTests(unittest.TestCase):
    def test_preserves_useful_line_breaks(self):
        raw = "Hello   world\n\nNext   line\r\nThird"
        out = app.normalize_caption_text(raw)
        self.assertEqual(out, "Hello world\nNext line\nThird")

    def test_empty(self):
        self.assertEqual(app.normalize_caption_text(""), "")
        self.assertEqual(app.normalize_caption_text("   \n  "), "")


class IntervalTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(app.validate_capture_interval("0.5"), 0.5)
        self.assertEqual(app.validate_capture_interval(1), 1.0)

    def test_rejects_nan_inf_zero_negative_excessive(self):
        for bad in (math.nan, math.inf, -math.inf, 0, -1, 0.1, 10, "abc", None):
            with self.assertRaises(ValueError):
                app.validate_capture_interval(bad)  # type: ignore[arg-type]


class SendGateTests(unittest.TestCase):
    def test_empty_preview_cannot_send(self):
        self.assertFalse(app.preview_has_sendable_text(""))
        self.assertFalse(
            app.can_enable_send(
                preview_text="",
                target_selected=True,
                target_valid=True,
                send_in_progress=False,
            )
        )

    def test_requires_valid_target(self):
        self.assertFalse(
            app.can_enable_send(
                preview_text="hello",
                target_selected=True,
                target_valid=False,
                send_in_progress=False,
            )
        )

    def test_disabled_while_sending(self):
        self.assertFalse(
            app.can_enable_send(
                preview_text="hello",
                target_selected=True,
                target_valid=True,
                send_in_progress=True,
            )
        )

    def test_ok_when_ready(self):
        self.assertTrue(
            app.can_enable_send(
                preview_text="hello",
                target_selected=True,
                target_valid=True,
                send_in_progress=False,
            )
        )


class FocusPasteSafetyTests(unittest.TestCase):
    def test_failed_focus_never_allows_paste(self):
        self.assertFalse(app.safe_focus_allows_paste(False, True))
        self.assertFalse(app.safe_focus_allows_paste(True, False))
        self.assertTrue(app.safe_focus_allows_paste(True, True))


class ClassifyTests(unittest.TestCase):
    def test_notepad_chatgpt_title_rejected(self):
        kind = app.classify_window_kind("ChatGPT", "notepad.exe")
        self.assertEqual(kind, "unknown")

    def test_unidentified_process_rejected(self):
        kind = app.classify_window_kind("ChatGPT", "")
        self.assertEqual(kind, "unknown")

    def test_verified_chatgpt_exe(self):
        kind = app.classify_window_kind("ChatGPT", "ChatGPT.exe")
        self.assertEqual(kind, "desktop_app")

    def test_browser_not_claimed_as_tab(self):
        kind = app.classify_window_kind("Google - Chrome", "chrome.exe")
        self.assertEqual(kind, "browser_window")

    def test_companion(self):
        kind = app.classify_window_kind("ChatGPT Companion", "chatgpt.exe")
        self.assertEqual(kind, "companion")


class TargetValidationTests(unittest.TestCase):
    def test_rejects_missing_handle(self):
        ok, reason = app.validate_target_snapshot(
            hwnd=None,
            is_window=False,
            is_visible=False,
            current_title="",
            current_pid=0,
            expected_pid=0,
            kind="desktop_app",
            process_name="chatgpt.exe",
        )
        self.assertFalse(ok)
        self.assertIn("No target", reason)

    def test_rejects_stale_handle(self):
        ok, reason = app.validate_target_snapshot(
            hwnd=123,
            is_window=False,
            is_visible=True,
            current_title="ChatGPT",
            current_pid=1,
            expected_pid=1,
            kind="desktop_app",
            process_name="chatgpt.exe",
        )
        self.assertFalse(ok)
        self.assertIn("no longer exists", reason)

    def test_rejects_invisible_target(self):
        ok, reason = app.validate_target_snapshot(
            hwnd=123,
            is_window=True,
            is_visible=False,
            current_title="ChatGPT",
            current_pid=1,
            expected_pid=1,
            kind="desktop_app",
            process_name="chatgpt.exe",
        )
        self.assertFalse(ok)
        self.assertIn("not visible", reason)

    def test_rejects_notepad_as_desktop(self):
        ok, reason = app.validate_target_snapshot(
            hwnd=123,
            is_window=True,
            is_visible=True,
            current_title="ChatGPT",
            current_pid=1,
            expected_pid=1,
            kind="desktop_app",
            process_name="notepad.exe",
        )
        self.assertFalse(ok)
        self.assertIn("verified ChatGPT", reason)

    def test_accepts_desktop_chatgpt(self):
        ok, _ = app.validate_target_snapshot(
            hwnd=123,
            is_window=True,
            is_visible=True,
            current_title="ChatGPT",
            current_pid=1,
            expected_pid=1,
            kind="desktop_app",
            process_name="chatgpt.exe",
        )
        self.assertTrue(ok)

    def test_rejects_wrong_chatgpt_tab(self):
        ok, reason = app.validate_target_snapshot(
            hwnd=123,
            is_window=True,
            is_visible=True,
            current_title="ChatGPT - Personal - Chrome",
            current_pid=1,
            expected_pid=1,
            kind="browser_tab",
            process_name="chrome.exe",
            tab_name="ChatGPT - Interview",
            active_tab_name="ChatGPT - Personal",
            tab_ordinal=0,
            active_tab_ordinal=1,
            page_url="https://chatgpt.com/",
            require_url=True,
        )
        self.assertFalse(ok)
        self.assertIn("not active", reason)

    def test_accepts_exact_tab_and_url(self):
        ok, _ = app.validate_target_snapshot(
            hwnd=123,
            is_window=True,
            is_visible=True,
            current_title="ChatGPT - Interview - Chrome",
            current_pid=1,
            expected_pid=1,
            kind="browser_tab",
            process_name="chrome.exe",
            tab_name="ChatGPT - Interview",
            active_tab_name="ChatGPT - Interview",
            tab_ordinal=2,
            active_tab_ordinal=2,
            page_url="https://chatgpt.com/c/abc",
            require_url=True,
        )
        self.assertTrue(ok)

    def test_rejects_lookalike_url(self):
        self.assertFalse(app.is_allowed_chatgpt_url("https://chatgpt.com.evil.example/"))
        self.assertFalse(app.is_allowed_chatgpt_url("https://not-chatgpt.com/"))
        self.assertFalse(app.is_allowed_chatgpt_url("http://chatgpt.com/"))
        self.assertTrue(app.is_allowed_chatgpt_url("https://chatgpt.com/"))
        self.assertTrue(app.is_allowed_chatgpt_url("https://chat.openai.com/chat"))


class TabMatchTests(unittest.TestCase):
    def test_any_chatgpt_tab_is_not_enough(self):
        self.assertFalse(
            app.tabs_match_exactly("ChatGPT - Interview", "ChatGPT - Personal")
        )

    def test_runtime_id_preferred(self):
        self.assertTrue(
            app.tabs_match_exactly(
                "ChatGPT",
                "ChatGPT",
                selected_runtime_id="[1,2,3]",
                active_runtime_id="[1,2,3]",
            )
        )
        self.assertFalse(
            app.tabs_match_exactly(
                "ChatGPT",
                "ChatGPT",
                selected_runtime_id="[1,2,3]",
                active_runtime_id="[9,9,9]",
            )
        )


class CloseSafetyTests(unittest.TestCase):
    def test_unidentified_window_does_not_click_arbitrary_button(self):
        self.assertFalse(
            app.close_may_click_button(
                window_verified_live_captions=False,
                button_looks_like_close=True,
            )
        )
        self.assertTrue(
            app.close_may_click_button(
                window_verified_live_captions=True,
                button_looks_like_close=True,
            )
        )


class GenerationTests(unittest.TestCase):
    def test_old_generation_messages_ignored(self):
        self.assertFalse(app.should_accept_capture_generation(1, 2))
        self.assertTrue(app.should_accept_capture_generation(2, 2))


class ExceptionCallbackTests(unittest.TestCase):
    def test_exception_message_survives_queued_callback(self):
        captured: list[str] = []
        try:
            raise RuntimeError("boom-detail")
        except Exception as e:
            captured.append(app.format_exception_message(e))
        self.assertIn("boom-detail", captured[0])


class AutoCaptureNeverSendsTests(unittest.TestCase):
    def test_auto_capture_worker_source_has_no_send_call(self):
        import inspect

        src = inspect.getsource(app.CaptionCopierApp._auto_capture_worker)
        self.assertNotIn("send_to_gpt", src)
        self.assertNotIn("_send_worker", src)
        self.assertNotIn("paste_and_enter", src)
        self.assertNotIn("ctrl+v", src.lower())
        # Live caption only — must not write send box
        self.assertNotIn("send_box", src)
        self.assertNotIn("_set_send_text", src)


class PreviewRefreshTests(unittest.TestCase):
    def test_suppress_unchanged(self):
        self.assertFalse(app.should_refresh_preview("same", "same"))
        self.assertTrue(app.should_refresh_preview("a", "b"))


class ClipboardSentinelTests(unittest.TestCase):
    def test_try_get_signature(self):
        # Without display may fail; just ensure callable returns tuple
        result = app.clipboard_try_get()
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[0], bool)


if __name__ == "__main__":
    unittest.main()
