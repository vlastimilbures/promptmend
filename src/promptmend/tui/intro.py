"""The About screen on `a` (#112): static text in a Textual border. (The intro screen that
used to open the interface was removed in #245.)"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from . import brand


class AboutScreen(ModalScreen[None]):
    """Version, install channel, runtime, folders, licence and repository; Escape, `a`, `q`
    or Close leaves it."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "Close"),
        Binding("a", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
    ]

    def __init__(self, facts: list[str]) -> None:
        super().__init__()
        self.facts = facts

    def compose(self) -> ComposeResult:
        width = self.app.size.width
        title = brand.LOGO if width > brand.NARROW else brand.TAGLINE
        with Vertical(classes="dialog", id="about"):
            yield Static(title, markup=False, classes="dialog-title")
            yield Static("\n".join(self.facts), markup=False, id="about-facts")
            yield Button("Close", id="about-close")

    def on_mount(self) -> None:
        self.query_one("#about-close", Button).focus()

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)
