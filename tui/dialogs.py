"""Shared modal dialogs and widgets for the storage and cleanup screens."""
import asyncio

from rich.text import Text
from textual import on, work
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, SelectionList, Static

from strings import TEXT
from system_steward import pretty_path, size_label


def display_path(path):
    return Text(pretty_path(path))


class CleanupList(SelectionList):
    BINDINGS = [Binding("space", "select", "Toggle group")]


class InfoDialog(ModalScreen):
    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, title, content):
        super().__init__()
        self.heading, self.content = title, content

    def compose(self):
        with Vertical(id="info-dialog", classes="dialog"):
            yield Label(self.heading, classes="dialog-title")
            with VerticalScroll():
                yield Static(Text(self.content))
            yield Button("Close", id="close-info", variant="primary")

    @on(Button.Pressed, "#close-info")
    def action_close(self):
        self.dismiss()


class ConfirmDialog(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "Cancel"), ("n", "cancel", "Cancel"), ("y", "confirm", "Confirm")]

    def __init__(self, kind, groups, amount, checker):
        super().__init__()
        self.kind, self.groups, self.amount, self.checker = kind, groups, amount, checker
        self.checking = False

    def compose(self):
        with Vertical(id="confirm-dialog", classes="dialog"):
            yield Label("REVIEW & CONFIRM", classes="eyebrow")
            yield Label(f"Clear {len(self.groups)} selected groups?", classes="dialog-title")
            yield Static(f"{size_label(self.amount)} measured · {self.kind} · permanent deletion", id="confirm-summary")
            with VerticalScroll(id="confirm-paths"):
                for group in self.groups:
                    yield Static(Text(f"• {group.label}\n  {pretty_path(group.path)}\n  {group.effect}\n"))
            yield Static("There is no undo. Close Adobe apps and Creative Cloud before continuing." if self.kind == "Adobe"
                         else "There is no undo. Close Chrome before continuing. Website-data groups sign you out.", id="confirm-warning")
            yield Static("Only the selected groups will be cleared.", id="confirm-status")
            yield Static("Y confirms · Esc cancels · Enter activates the focused button", classes="confirmation-hint")
            with Horizontal(classes="dialog-actions"):
                yield Button("Go back", id="cancel-delete")
                yield Button("Permanently clear", variant="error", id="confirm-delete")

    def on_mount(self):
        self.query_one("#cancel-delete", Button).focus()
        self.update_confirmation()

    def update_confirmation(self):
        small = self.app.size.width < 64 or self.app.size.height < 24
        self.query_one("#confirm-delete", Button).disabled = self.checking or small
        self.query_one("#cancel-delete", Button).disabled = self.checking

    def on_resize(self):
        self.set_class(self.app.size.height < 32, "short")
        if self.is_mounted:
            self.update_confirmation()

    @on(Button.Pressed, "#cancel-delete")
    def action_cancel(self):
        if not self.checking:
            self.dismiss(False)

    @on(Button.Pressed, "#confirm-delete")
    @work(exclusive=True)
    async def action_confirm(self):
        if self.checking or self.app.size.width < 64 or self.app.size.height < 24:
            return
        self.checking = True
        self.update_confirmation()
        status = self.query_one("#confirm-status", Static)
        status.update("Checking that applications are closed…")
        try:
            running = await asyncio.to_thread(self.checker)
            if running:
                status.update(f"{self.kind} apps or helpers are still running. To quit each one, {TEXT.quit_app}, then retry.")
            else:
                self.dismiss(True)
                return
        except OSError as exc:
            status.update(Text(str(exc)))
        finally:
            self.checking = False
            if self.is_mounted:
                self.update_confirmation()
