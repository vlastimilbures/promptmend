"""The Try tab: rewrite a draft typed here, the way a trigger would, and read the
result before any trigger pastes one.

The draft is typed into the tab; the clipboard is never read or written. The rewrite runs in
process, in a worker thread, through try_run.run() and pipeline.rewrite() (so the draft never
appears in a command line or the process list), and with it through factory.make_provider() and
its data-protection gate; this pane only reads its widgets, loads the settings, asks, and shows
the outcome. "Run against" starts at the real provider when try_run.usable() says the picked
provider could rewrite (its key set or none needed, PROMPT_LOCAL_ONLY not refusing it), else at
the dry run; it follows the provider picker until a target is picked by hand, which then stays,
a reload included. A dry run goes to the set-up smoke test's stub on 127.0.0.1 with a
placeholder key: no provider is called, nothing is recorded, and its usage line and report say
it is no rewrite. A real provider call always first asks, naming the provider and the model,
whatever the picker started at, and is then recorded in the usage history as a direct call (no
trigger), as `promptmend improve` typed in a terminal would be. The result is shown as plain
text, never read as markup.
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
from ..providers.registry import PROVIDERS
from . import teach
from .base import Pane, button_row
from .modals import ConfirmModal

if TYPE_CHECKING:
    from .state import State

STUB = "stub"
REAL = "real"
TARGETS = ((try_run.DRY_RUN, STUB), ("Real provider", REAL))
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
    # The confirm dialog of a real run is open: one at a time, so one Send is one call.
    confirming = False
    # The provider picker starts at the configured one, once: a reload after a change
    # elsewhere keeps what was picked here.
    defaulted = False
    # The stub's request count of the last stub run (0 when the gate stopped it first).
    stub_requests: int | None = None
    # "Run against" was picked by hand: it no longer follows the provider picker.
    target_picked = False
    # The target _default_target() last set (the picker starts at the dry run).
    target_default = STUB

    def compose(self) -> ComposeResult:
        yield Static(
            "Type a draft and rewrite it as a trigger would. The clipboard is never read. "
            "Real provider asks first, goes through the data-protection gate and is recorded "
            "in the usage history as -try-. Dry run, the default while the provider has no "
            "key or is refused, checks the wiring: a stub on 127.0.0.1 answers, no rewrite.",
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
                with Vertical(classes="try-option", id=f"{id_}-option"):
                    yield Label(label)
                    # Compact (borderless, one line): four fit one row at 70 columns with no
                    # value wrapped (#394; their widths are in app.CSS).
                    yield Select(options, value=value, allow_blank=False, compact=True, id=id_)
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
        # A key set elsewhere since: the default follows, unless a target was picked here.
        self._default_target(state)

    def _default_target(self, state: State | None) -> None:
        """Start "Run against" at the real provider when the picked one is usable, else at
        the dry run; a target picked by hand is kept. Only sets the picker: Run still asks
        before a real call."""
        if self.target_picked or state is None:
            return
        provider = self._value("#try-provider")
        real = try_run.usable(provider, state.settings)
        self.target_default = REAL if real else STUB
        self.query_one("#try-target", Select).value = self.target_default

    @on(Select.Changed, "#try-provider")
    def _provider_changed(self) -> None:
        self._default_target(self.state)

    @on(Select.Changed, "#try-target")
    def _target_changed(self, event: Select.Changed) -> None:
        """A pick by hand: a value that is not the default set last and still the picker's.
        The events of the defaults set here (and the picker's first value, posted on mount)
        are either the default or already overtaken by a later one, so they never count.

        The limit: a reload whose show() runs between a pick and its event reaching this pane
        (a few message hops) puts the default back, and the pick is lost; picking again
        keeps it."""
        if event.value != self.target_default and event.value == event.select.value:
            self.target_picked = True

    def _value(self, selector: str) -> str:
        return str(self.query_one(selector, Select).value)

    @on(Button.Pressed, "#try-run")
    def _run(self) -> None:
        if self.running or self.confirming:
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
            self.confirming = False
            if yes:
                self._start(draft, provider, profile, cfg, command)

        self.confirming = True
        self.app.push_screen(
            ConfirmModal(
                f"Send this draft to {provider}?",
                self._preview(layers, cfg, provider, tier, profile, len(draft)),
                confirm="Send",
            ),
            done,
        )

    def _preview(
        self,
        layers: ConfigLayers,
        cfg: Settings,
        provider: str,
        tier: str,
        profile: str | None,
        length: int,
    ) -> str:
        """What the Send dialog names. The profile whole, since the closed picker cuts a long
        name; "as configured" with what it resolves to for this call. cfg is for_call()'s:
        PROMPT_PRO_PROFILE when set, on the pro tier of a provider that has one, else
        PROMPT_PROFILE. Whichever applied is shown as Model and Base URL are
        (common.shown_value(): a key-like value withheld, a control character as its repr)."""
        model = model_setting(provider, tier)
        url = PROVIDERS[provider].url_setting
        shown_profile = profile
        if not shown_profile:
            # The setting cfg's profile came from: PROMPT_PROFILE's value unless for_call()
            # swapped in another.
            name = "PROMPT_PROFILE"
            if cfg.profile != layers.settings().profile:
                name = "PROMPT_PRO_PROFILE"
            shown_profile = f"{common.shown_value(name, layers.entries[name])} (as configured)"
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
                f"Profile: {shown_profile}",
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
        if self.running:  # one run at a time, however it was asked for
            return
        self.running = True
        self.query_one("#try-run", Button).disabled = True
        doing = (
            f"{try_run.DRY_RUN} against the stub on 127.0.0.1…"
            if command is None
            else f"Rewriting with {provider}…"
        )
        self.query_one("#try-usage", Static).update(doing)
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
            done = (
                f"{try_run.DRY_RUN.lower()}, the stub answered (no rewrite)"
                if command is None
                else "rewritten"
            )
            self.report(f"Done: {done}.", command=command)

    def failed(self, exc: Exception) -> None:
        self.running = False
        self.query_one("#try-run", Button).disabled = False
        super().failed(exc)
