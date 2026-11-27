"""The Try tab: rewrite a draft typed here, the way a trigger would, and read the
result before any trigger pastes one.

The draft is typed into the tab; the clipboard is never read or written. The rewrite runs in
process, in a worker thread, through try_run.run() and pipeline.rewrite() (so the draft never
appears in a command line or the process list), and with it through factory.make_provider() and
its data-protection gate; this pane only reads its widgets, loads the settings, asks, and shows
the outcome. By default it runs against the set-up smoke test's stub on 127.0.0.1
with a placeholder key: no provider is called and nothing is recorded. A real provider call
first asks, naming the provider and the model, and is then recorded in the usage history as
a direct call (no trigger), as `promptmend improve` typed in a terminal would be. The result
is shown as plain text, never read as markup.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label, Select, Static, TextArea

from .. import pipeline, sanitize, try_run
from ..commands import common
from ..config import TIERS, ConfigLayers, Settings, check_provider_options, model_setting
from ..factory import PROVIDER_NAMES
from ..prompt_builder import ADDED, PROFILES
from . import teach
from .base import Pane, button_row
from .modals import ConfirmModal

if TYPE_CHECKING:
    from .state import State

STUB = "stub"
REAL = "real"
TARGETS = (("Local stub", STUB), ("Real provider", REAL))
# The profile picker's first choice: no --profile, so the call uses PROMPT_PROFILE (or, on the
# pro tier, PROMPT_PRO_PROFILE when set), exactly as a trigger that names none.
CONFIGURED = "(configured)"
# What the session log shows in place of the draft, which never leaves this tab.
DRAFT_WITHHELD = "<draft withheld>"


class DraftArea(TextArea):
    """The draft. It keeps every key typed into it (Tab moves on); Escape leaves it, so the
    tab keys work again."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "leave", "Leave")]

    def action_leave(self) -> None:
        self.screen.set_focus(None)


def _profiles(names: Sequence[str]) -> list[tuple[str, str]]:
    return [("as configured", CONFIGURED), *((n, n) for n in names)]


class TryPane(Pane):
    # A run is under way (one at a time; Run is disabled meanwhile).
    running = False
    # The provider picker starts at the configured one, once: a reload after a change
    # elsewhere keeps what was picked here.
    defaulted = False
    # The stub's request count of the last stub run (0 when the gate stopped it first).
    stub_requests: int | None = None

    def compose(self) -> ComposeResult:
        yield Static(
            "Type a draft and rewrite it as a trigger would. The clipboard is never read. "
            "Local stub answers on 127.0.0.1 and calls no provider; a real provider asks "
            "first, goes through the data-protection gate and is recorded in the usage "
            "history as -try-.",
            classes="note",
            markup=False,
        )
        yield DraftArea(
            id="try-draft", placeholder="Your draft (Escape leaves the box)", soft_wrap=True
        )
        with Horizontal(id="try-options"):
            for id_, label, options, value in (
                ("try-target", "Run against", TARGETS, STUB),
                ("try-provider", "Provider", [(n, n) for n in PROVIDER_NAMES], PROVIDER_NAMES[0]),
                ("try-profile", "Profile", _profiles(list(PROFILES)), CONFIGURED),
                ("try-tier", "Tier", [(t, t) for t in TIERS], TIERS[0]),
            ):
                with Vertical(classes="try-option"):
                    yield Label(label)
                    yield Select(options, value=value, allow_blank=False, id=id_)
        yield button_row(("try-run", "Run"))
        yield Static("No run yet.", id="try-usage", markup=False)
        yield TextArea("", id="try-result", read_only=True, soft_wrap=True)
        yield self.result()

    def show(self, state: State) -> None:
        profiles = self.query_one("#try-profile", Select)
        picked = profiles.value
        names = [*PROFILES, *(p.name for p in state.profiles if p.status == ADDED)]
        profiles.set_options(_profiles(names))
        if not self.defaulted:
            self.defaulted = True
            providers = self.query_one("#try-provider", Select)
            if state.settings.provider in PROVIDER_NAMES:
                providers.value = state.settings.provider
        if picked in names:
            profiles.value = picked
        else:
            profiles.value = CONFIGURED

    def _value(self, selector: str) -> str:
        return str(self.query_one(selector, Select).value)

    @on(Button.Pressed, "#try-run")
    def _run(self) -> None:
        if self.running:
            return
        draft = self.query_one("#try-draft", TextArea).text
        target = self._value("#try-target")
        provider, tier = self._value("#try-provider"), self._value("#try-tier")
        picked = self._value("#try-profile")
        profile = None if picked == CONFIGURED else picked
        try:
            # Strict, as a trigger loads them: a bad PROMPT_LOCAL_ONLY, PROMPT_EXTRA_PATTERNS
            # or a broken config.toml fails closed with the marker instead of falling back
            # (repair mode would run with the default, an ungated or unrefused call).
            layers = ConfigLayers.resolve()
            cfg = layers.settings().for_call(tier, provider=provider)
            check_provider_options(provider, tier, None)
        except Exception as exc:
            refused = try_run.Outcome(sanitize.clean(pipeline.marker(exc)), True, "", None)
            self._finish(refused, None)
            return
        if target == STUB:
            self._start(draft, provider, profile, cfg, None)
            return
        command = teach.equivalent(
            "improve",
            "--provider",
            provider,
            *(("--profile", profile) if profile else ()),
            "--tier",
            tier,
            "--source",
            "argument",
            "--text",
            DRAFT_WITHHELD,
        )

        def done(yes: bool | None) -> None:
            if yes:
                self._start(draft, provider, profile, cfg, command)

        self.app.push_screen(
            ConfirmModal(
                f"Send this draft to {provider}?",
                self._preview(layers, cfg, provider, tier, len(draft)),
                confirm="Send",
            ),
            done,
        )

    def _preview(
        self, layers: ConfigLayers, cfg: Settings, provider: str, tier: str, length: int
    ) -> str:
        model = model_setting(provider, tier)
        url = f"{provider.upper()}_BASE_URL"
        recorded = (
            "It is recorded in the usage history as -try- (a direct call, no trigger)."
            if cfg.history
            else "PROMPT_HISTORY is off: nothing is recorded."
        )
        return "\n".join(
            [
                f"Provider: {provider}",
                f"Model: {common.shown_value(model, layers.entries[model])}",
                f"Base URL: {common.shown_value(url, layers.entries[url])}",
                f"Draft: {length} characters, typed here (the clipboard is not read)",
                "",
                "It goes through the data-protection gate as a trigger's draft does. A cloud "
                "provider may charge for the call.",
                recorded,
            ]
        )

    def _start(
        self, draft: str, provider: str, profile: str | None, cfg: Settings, command: str | None
    ) -> None:
        self.running = True
        self.query_one("#try-run", Button).disabled = True
        where = "the local stub" if command is None else provider
        self.query_one("#try-usage", Static).update(f"Rewriting with {where}…")
        self.background(lambda: self._call(draft, provider, profile, cfg, command))

    def _call(
        self, draft: str, provider: str, profile: str | None, cfg: Settings, command: str | None
    ) -> None:
        """In the worker: one run (try_run.run()). ``command`` is None for a stub run; the
        outcome is shown before a real run is recorded, and the History tab then reloads."""
        outcome = try_run.run(
            draft,
            provider,
            profile,
            cfg,
            stub=command is None,
            show=lambda shown: self.app.call_from_thread(self._finish, shown, command),
        )
        if outcome.recorded:
            # The History tab shows the new call.
            self.app.call_from_thread(self.manage.reload)

    def _finish(self, outcome: try_run.Outcome, command: str | None) -> None:
        self.running = False
        self.query_one("#try-run", Button).disabled = False
        self.query_one("#try-result", TextArea).load_text(outcome.text)
        self.query_one("#try-usage", Static).update(outcome.usage)
        if command is None:
            self.stub_requests = outcome.requests
        if outcome.error:
            self.report(outcome.text, error=True, command=command)
        else:
            done = "the local stub answered" if command is None else "rewritten"
            self.report(f"Done: {done}.", command=command)

    def failed(self, exc: Exception) -> None:
        self.running = False
        self.query_one("#try-run", Button).disabled = False
        super().failed(exc)
