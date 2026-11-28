"""The Triggers tab: every trigger with its provider and state, and the deploy, diff and
detach dialogs (`espanso deploy|detach`).

The Provider and Profile cells show the value a trigger call runs with (#396), resolved as
Home's rows resolve it (home.trigger_provider(), home.trigger_profile()); a value a setting
supplies has that setting's name under it, muted, and is drawn through common.shown_value().
Two lines rather than `value (SETTING)` on one, so the State column stays on screen at 80
columns."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.widgets import Button, DataTable, Static

from .. import deploy, doctor
from ..commands import common
from ..config import Entry
from ..factory import PROVIDER_LABELS
from . import teach
from .base import Pane, button_row, manifest_digest, plan_digest
from .home import Resolved, trigger_profile, trigger_provider
from .modals import Field, FormModal, TextModal
from .state import State, current_plan


def _servers_down(state: State) -> set[str]:
    """The local providers (ollama, lmstudio) whose server did not answer doctor's
    local_servers check; asked there once, never again here."""
    for check in state.report.checks:
        if check.id == "local_servers":
            servers = check.data.get("servers", {})
            return {name for name, info in servers.items() if info.get("answers") is False}
    return set()


def hinted(resolved: Resolved, entries: Mapping[str, Entry]) -> Text:
    """A Provider or Profile cell: the value a call runs with and, under it (muted), the
    setting that supplied it. The resolved value is drawn, not the setting's raw text (an
    empty or `OpenAI` PROMPT_PRO_PROVIDER runs openrouter or openai, as Home's Pro row
    says), through common.shown_value() with that setting's source, so a key-like or
    control-character value is never drawn raw. A value the trigger's command line names
    stands alone."""
    if resolved.setting is None:
        return Text(resolved.value)
    entry = dataclasses.replace(entries[resolved.setting], value=resolved.value)
    cell = Text(common.shown_value(resolved.setting, entry))
    cell.append(f"\n{resolved.setting}", style="dim")
    return cell


class TriggersPane(Pane):
    # Each shows or changes what the plan compares: they wait for the full State.
    FULL_ONLY = ("show-diff", "deploy", "detach")

    def compose(self) -> ComposeResult:
        yield Static(
            "A name under a value is the setting it comes from; a value alone is fixed in the "
            "trigger's command line, whatever PROMPT_PROVIDER says. A local trigger stays "
            "deployed while its server is down: State says what it needs.",
            classes="note",
            markup=False,
        )
        yield DataTable(id="triggers", cursor_type="none", zebra_stripes=True)
        # The buttons and their result right under the table, before the deploy target, whose
        # paths can wrap to several lines: Deploy stays on screen however long they are.
        yield button_row(
            ("show-diff", "Show diff"),
            ("deploy", "Deploy"),
            ("detach", "Detach"),
        )
        yield self.result()
        yield Static("", markup=False, id="deploy-target")

    def setup(self) -> None:
        self.query_one("#triggers", DataTable).add_columns(
            "Trigger", "Provider", "Tier", "Profile", "State", "File"
        )

    def show(self, state: State) -> None:
        states = {s.name: s.state for s in state.plan.steps} if state.plan else {}
        # A quick State has no plan yet: each file is being compared.
        unread = "checking" if state.failed is None else "not checked"
        unknown = "unknown" if state.full else unread
        down = _servers_down(state)
        stale = doctor.stale_pro_pins(state.deployed or [], state.settings)
        cfg, entries = state.settings, state.layers.entries
        table = self.query_one("#triggers", DataTable)
        table.clear()
        for t in state.triggers:
            provider: Text
            profile: Text
            if t.command == "improve":
                tier = t.tier or "standard"
                chosen = trigger_provider(cfg, tier, t.provider)
                runs = chosen.value
                provider = hinted(chosen, entries)
                if t.trigger in stale:  # an older deployed -ip-: what Espanso runs for now
                    runs = stale[t.trigger]
                    provider = Text(f"{runs} until redeployed")
                profile = hinted(trigger_profile(cfg, tier, runs, t.profile), entries)
            else:
                words = "prints PROMPT_PERSONA" if t.command == "persona" else "static snippet"
                provider, profile, tier = Text(words), Text("-"), "-"
            where = states.get(t.file, unknown) if t.active else "commented out"
            # Once its file is there, what typing it needs beats the file's state.
            deployed = where not in (deploy.MISSING, unknown)
            if deployed and t.active and t.command == "improve" and t.provider in down:
                where = f"needs {PROVIDER_LABELS[t.provider]}"
            # A hinted cell is two lines: the row grows to fit it (height None).
            cells = (Text(t.trigger), provider, Text(tier), profile, Text(where), Text(t.file))
            table.add_row(*cells, height=None)
        target = self.query_one("#deploy-target", Static)
        if not state.full:
            target.update(
                "Checking the match files and Espanso… Show diff, Deploy and Detach wait for it."
                if state.failed is None
                else f"Not compared with Espanso: the checks could not run ({state.failed})."
            )
        elif state.plan is None:
            target.update(f"Cannot compare with Espanso: {state.plan_error}")
        else:
            text = (
                f"Espanso match folder: {state.plan.espanso_dir / 'match'}\n"
                f"Launcher: {state.plan.launcher}"
            )
            if state.plan.yours:
                names = ", ".join(p.name for p in state.plan.yours)
                text += f"\nYour own match files (never touched by deploy or detach): {names}"
            espanso = next((c for c in state.report.checks if c.id == "espanso"), None)
            if espanso and (espanso.data.get("found") is False or espanso.data.get("query_failed")):
                # Deploy and detach then use the default folder: never silently.
                text += f"\n{espanso.message}"
            target.update(text)

    @on(Button.Pressed, "#show-diff")
    def _diff(self) -> None:
        if self.waits():
            return
        plan = self.state.plan if self.state else None
        if plan is None:
            self.report("No plan: Espanso's folder or this install's launcher was not found.")
            return
        diff = "".join(s.diff() for s in plan.steps if s.state != deploy.IN_SYNC)
        self.app.push_screen(
            TextModal("What deploy would change", diff or "Every match file is in sync.")
        )

    @on(Button.Pressed, "#deploy")
    def _deploy(self) -> None:
        self.preview_deploy()

    def preview_deploy(self) -> None:
        """Read the plan in a worker, then preview it in a dialog (Deploy, and
        Home's button for a deploy)."""
        if self.claim():
            self.report("Reading the match files…")
            self.background(lambda: self.app.call_from_thread(self._ask_deploy, current_plan()))

    def claim(self) -> bool:
        """Start a deploy or detach unless one is already open: a second dialog would apply
        a plan the first one already changed. Never on the quick State."""
        if self.waits():
            return False
        if self.busy:
            self.report("A deploy or detach is already in progress; finish or cancel it first.")
            return False
        self.busy = True
        return True

    def _ask_deploy(self, plan: deploy.Plan) -> None:
        if plan.is_noop:
            self.busy = False
            self.report("Nothing to do: every match file is in sync.")
            return
        if plan.only_forgets:  # the manifest only: no file is written, so nothing to ask
            self.report("Forgetting deploy records of files that are gone…")
            self.background(lambda: self._apply(plan, {}))
            return
        lines = [f"Into {plan.espanso_dir / 'match'}:"]
        lines += [f"{s.state:<9} {s.name}" for s in plan.steps]
        lines += [f"forget    {key} (already gone)" for key in plan.orphans]
        diffs = [s.diff() for s in plan.steps if s.state not in (deploy.IN_SYNC, deploy.MISSING)]
        preview = "\n".join(lines) + ("\n\n" + "".join(diffs) if diffs else "")
        conflicts = plan.conflicts
        fields = [
            Field(
                f"choice-{i}",
                f"{s.name} is {s.state}: keep yours, take ours (yours backed up) or write ours "
                "side by side",
                deploy.CHOICES,
                value=deploy.KEEP,
            )
            for i, s in enumerate(conflicts)
        ]

        def done(values: dict[str, str] | None) -> None:
            if values is None:
                self.busy = False
                return
            choices = {s.name: values[f"choice-{i}"] for i, s in enumerate(conflicts)}
            self.report("Deploying…")
            self.background(lambda: self._apply(plan, choices))

        self.app.push_screen(
            FormModal("Deploy the match files", fields, submit="Deploy", preview=preview), done
        )

    def _apply(self, plan: deploy.Plan, choices: dict[str, str]) -> None:
        """In the worker: apply the plan the person saw, unless the files changed since."""
        if plan_digest(current_plan()) != plan_digest(plan):
            raise deploy.DeployError(
                "the match files or the deploy record changed since the preview; nothing was "
                "written. Press Deploy again to see the new plan"
            )
        outcome = deploy.apply(plan, choices)
        # The files are written: show them now, not after the restart, which can take a
        # while. _done() reloads again once Espanso answered.
        self.app.call_from_thread(self.manage.reload)
        lines = list(outcome.lines)
        if outcome.changed and not deploy.restart_espanso():
            lines.append("Could not restart Espanso; run `espanso restart` yourself.")
        if outcome.kept:
            names = ", ".join(p.name for p in outcome.kept)
            lines.append(f"WARNING: kept as you have them and NOT updated: {names}.")
        else:
            lines.append("The match files are up to date.")
        self.app.call_from_thread(
            self._done,
            "\n".join(lines),
            bool(outcome.kept),
            teach.equivalent("espanso", "deploy"),
        )

    def _done(self, message: str, error: bool, command: str | None = None) -> None:
        self.busy = False
        self.report(message, error=error, command=command)
        self.manage.reload()

    @on(Button.Pressed, "#detach")
    def _ask_detach_start(self) -> None:
        self.ask_detach()

    def ask_detach(self) -> None:
        """Read the deploy record in a worker, then ask (`espanso detach`)."""
        if self.claim():
            self.background(
                lambda: self.app.call_from_thread(
                    self._ask_detach,
                    deploy.Manifest.load(),
                    deploy.espanso_dir().resolve(),
                )
            )

    def _ask_detach(self, manifest: deploy.Manifest, root: Path) -> None:
        if not manifest.entries:
            self.busy = False
            self.report("Nothing to do: promptmend has no deployed match files on record.")
            return
        preview = "Detach removes the files below that you have not edited:\n" + "\n".join(
            f"  {target}" for target in sorted(manifest.entries)
        )
        fields = [
            Field(
                "mode",
                "keep-static: only the matches that call the CLI; remove-all: every file",
                ("keep-static", "remove-all"),
                value="keep-static",
            )
        ]

        def done(values: dict[str, str] | None) -> None:
            if values is None:
                self.busy = False
                return
            remove_all = values["mode"] == "remove-all"
            self.background(lambda: self._detach_now(manifest, root, remove_all))

        self.app.push_screen(FormModal("Detach", fields, submit="Detach", preview=preview), done)

    def _detach_now(self, manifest: deploy.Manifest, root: Path, remove_all: bool) -> None:
        if manifest_digest(deploy.Manifest.load()) != manifest_digest(manifest):
            raise deploy.DeployError(
                "the deploy record changed since the preview; nothing was removed. Press "
                "Detach again"
            )
        outcome = deploy.detach(manifest, root, remove_all=remove_all)
        self.app.call_from_thread(self.manage.reload)  # as in _apply(), before the restart
        lines = list(outcome.lines)
        if outcome.changed and not deploy.restart_espanso():
            lines.append("Could not restart Espanso; run `espanso restart` yourself.")
        argv = ("espanso", "detach", *(["--remove-all"] if remove_all else []))
        self.app.call_from_thread(self._done, "\n".join(lines), False, teach.equivalent(*argv))
