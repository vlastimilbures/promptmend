"""The interface's dialogs: a confirmation, a read-only text view (a diff), a small form, a
list to pick from (a setting's choices) and one value to edit (a setting). The last two can
offer a Use default button, which returns USE_DEFAULT: the caller resets the setting.

A destructive action always goes through one of them: nothing is deleted, overwritten or
deployed until its button is pressed, and Cancel (or Escape, or n) is the focused default.
The confirmation always opens on Cancel. A form does too, unless its caller names a field to
focus (``focus=``), which only a form that deletes nothing does: Set key, Test call, Export,
the default profile, and the days to prune (a confirmation on Cancel follows it). The pick
list and the edit dialog, which change nothing until a value is picked or saved, focus their
list or field.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import ClassVar, Final

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from . import teach


class UseDefault(enum.Enum):
    """What a pick or edit dialog returns for its Use default button."""

    USE_DEFAULT = "use-default"


USE_DEFAULT: Final = UseDefault.USE_DEFAULT


@dataclass(frozen=True)
class Field:
    """One form field: a choice when ``options`` is set, else a text input (``secret`` hides
    what is typed)."""

    id: str
    label: str
    options: Sequence[str] = ()
    value: str = ""
    secret: bool = False
    placeholder: str = ""


class _Dialog[T](ModalScreen[T]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, preview: str, cancel_value: T) -> None:
        super().__init__()
        self.dialog_title = title
        self.preview = preview
        self.cancel_value = cancel_value

    def heading(self) -> ComposeResult:
        yield Label(self.dialog_title, classes="dialog-title")
        if self.preview:
            with VerticalScroll(classes="preview"):
                yield Static(self.preview, markup=False)

    def action_cancel(self) -> None:
        self.dismiss(self.cancel_value)

    def cancel_row(self, *, use_default: bool) -> ComposeResult:
        """Cancel, and Use default when asked for (its tooltip names `config unset`)."""
        yield Button("Cancel", id="cancel")
        if use_default:
            yield Button("Use default", id="use-default", tooltip=teach.tooltip("use-default"))


class ConfirmModal(_Dialog[bool]):
    """Asks before a change; True only when the confirm button (or y) is pressed."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel"),
        Binding("n", "cancel", "No"),
        Binding("y", "confirm", "Yes"),
    ]

    def __init__(self, title: str, preview: str = "", confirm: str = "Yes") -> None:
        super().__init__(title, preview, False)
        self.confirm_label = confirm

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.heading()
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.confirm_label, id="confirm", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def action_confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)


class TextModal(_Dialog[None]):
    """Shows text, such as a diff, to read; changes nothing."""

    def __init__(self, title: str, text: str) -> None:
        super().__init__(title, text, None)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.heading()
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self.query_one("#close", Button).focus()

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.dismiss(None)


class PickModal(_Dialog[str | UseDefault | None]):
    """A list to pick one line from (a setting's choices); returns the picked value, or None when
    cancelled. Picking changes nothing by itself, so the list has the focus when it opens
    (Enter or a click picks); Escape or Cancel closes it. With ``use_default`` a Use default
    button returns USE_DEFAULT."""

    def __init__(
        self,
        title: str,
        choices: Sequence[tuple[str, str]],
        preview: str = "",
        *,
        selected: int | None = None,
        use_default: bool = False,
    ) -> None:
        super().__init__(title, preview, None)
        self.use_default = use_default
        # (label, value) pairs; a label is plain text, never markup.
        self.choices = choices
        # The choice highlighted as it opens (a setting's current value).
        self.selected = selected

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.heading()
            yield OptionList(
                *(Option(Text(label), id=str(n)) for n, (label, _) in enumerate(self.choices)),
                id="pick",
            )
            with Horizontal(classes="buttons"):
                yield from self.cancel_row(use_default=self.use_default)

    def on_mount(self) -> None:
        pick = self.query_one("#pick", OptionList)
        if self.selected is not None:
            pick.highlighted = self.selected
        pick.focus()

    @on(OptionList.OptionSelected, "#pick")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.choices[event.option_index][1])

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#use-default")
    def _use_default(self) -> None:
        self.dismiss(USE_DEFAULT)


class EditModal(_Dialog[str | UseDefault | None]):
    """One value to type, prefilled (a setting); returns it, or None when cancelled.
    ``check`` reads it as the CLI would: its message (a parser error) stays in the dialog, in
    the error colour, and nothing is returned until the value passes. The field has the focus:
    Enter there saves, Escape cancels. With ``use_default`` a Use default button returns
    USE_DEFAULT, whatever the field holds."""

    def __init__(
        self,
        title: str,
        value: str,
        check: Callable[[str], str | None],
        preview: str = "",
        *,
        use_default: bool = False,
    ) -> None:
        super().__init__(title, preview, None)
        self.value = value
        self.check = check
        self.use_default = use_default

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.heading()
            yield Input(self.value, id="edit-value")
            yield Static("", id="edit-error", markup=False)
            with Horizontal(classes="buttons"):
                yield from self.cancel_row(use_default=self.use_default)
                yield Button("Save", id="submit", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#edit-value", Input).focus()

    @on(Button.Pressed, "#submit")
    @on(Input.Submitted)
    def _submit(self) -> None:
        value = self.query_one("#edit-value", Input).value
        problem = self.check(value)
        if problem is None:
            self.dismiss(value)
            return
        self.query_one("#edit-error", Static).update(problem)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#use-default")
    def _use_default(self) -> None:
        self.dismiss(USE_DEFAULT)


class FormModal(_Dialog[dict[str, str] | None]):
    """A few fields and a submit button; returns the values by field id, or None when
    cancelled. Its submit button is the confirmation for what the preview describes, except
    prune and remove-key, which ask once more.

    Cancel has the focus when it opens, so Enter right after opening changes nothing. A
    caller whose submit deletes nothing passes ``focus``, a field id, to open in that field
    instead: Enter in a text field submits the form, and on a choice opens its list."""

    def __init__(
        self,
        title: str,
        fields: Sequence[Field],
        *,
        submit: str = "Save",
        preview: str = "",
        focus: str | None = None,
    ) -> None:
        super().__init__(title, preview, None)
        if focus is not None and focus not in {item.id for item in fields}:
            raise ValueError(f"no field {focus!r} to focus")
        self.fields = fields
        self.submit = submit
        self.focus_field = focus

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield from self.heading()
            for item in self.fields:
                yield Label(item.label)
                if item.options:
                    yield Select(
                        [(option, option) for option in item.options],
                        value=item.value if item.value in item.options else item.options[0],
                        allow_blank=False,
                        id=item.id,
                    )
                else:
                    yield Input(
                        item.value,
                        placeholder=item.placeholder,
                        password=item.secret,
                        id=item.id,
                    )
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.submit, id="submit", variant="primary")

    def on_mount(self) -> None:
        # Cancel unless the caller named a field: Enter right after opening changes nothing.
        target = f"#{self.focus_field}" if self.focus_field else "#cancel"
        self.query_one(target).focus()

    def values(self) -> dict[str, str]:
        found = {}
        for item in self.fields:
            if item.options:
                found[item.id] = str(self.query_one(f"#{item.id}", Select).value)
            else:
                found[item.id] = self.query_one(f"#{item.id}", Input).value
        return found

    @on(Button.Pressed, "#submit")
    @on(Input.Submitted)
    def _submit(self) -> None:
        self.dismiss(self.values())

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)
