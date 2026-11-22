"""Compatibility entry point: the interface lives in the ``tui`` package.

``from system_steward_tui import SystemStewardApp`` keeps working for launchers and scripts.
"""
from tui.app import ORDER_INTERVAL, SystemStewardApp
from tui.cleanup_screen import JOB_EXIT_GRACE, CleanupScreen
from tui.dialogs import CleanupList, ConfirmDialog, InfoDialog, display_path

__all__ = ["SystemStewardApp", "CleanupScreen", "ConfirmDialog", "InfoDialog", "CleanupList", "display_path",
           "ORDER_INTERVAL", "JOB_EXIT_GRACE"]
