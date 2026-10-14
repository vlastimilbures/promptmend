"""The Triggers tab: every trigger with its provider and state, and the deploy, diff and
detach dialogs (`espanso deploy|detach`)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.widgets import Button, DataTable, Static

from .. import deploy
from ..factory import PROVIDER_LABELS
from . import teach
from .base import Pane, add_row, button_row, manifest_digest, plan_digest
from .modals import Field, FormModal, TextModal
from .state import State, current_plan


@dataclass(frozen=True)
class DeployTarget:
    """Where a deploy goes: this install's plan, or setup's --espanso-dir/--launcher one
    (the previous install screen opened from the wizard), with its --no-restart and the
    options its logged `espanso deploy` names."""

    read: Callable[[], deploy.Plan] = current_plan
    no_restart: bool = False
    args: tuple[str, ...] = ()


def _servers_down(state: State) -> set[str]:
    """The local providers (ollama, lmstudio) whose server did not answer doctor's
    local_servers check (#220); asked there once, never again here."""
    for check in state.report.checks:
        if check.id == "local_servers":
            servers = check.data.get("servers", {})
            return {name for name, info in servers.items() if info.get("answers") is False}
    return set()


class TriggersPane(Pane):
    def compose(self) -> ComposeResult:
        yield Static(
            "Each trigger names its provider in its command line, so PROMPT_PROVIDER does not "
            "change these. An empty PROMPT_PRO_PROFILE means PROMPT_PROFILE. A local trigger "
            "stays deployed while its server is down: State says what it needs.",
            classes="note",
            markup=False,
        )
        yield DataTable(id="triggers", cursor_type="none", zebra_stripes=True)
        yield Static("", markup=False, id="deploy-target")
        yield button_row(
            ("show-diff", "Show diff"),
            ("deploy", "Deploy or repair"),
            ("detach", "Detach"),
        )
        yield self.result()

    def setup(self) -> None:
        self.query_one("#triggers", DataTable).add_columns(
            "Trigger", "Provider", "Tier", "Profile", "State", "File"
        )

    def show(self, state: State) -> None:
        states = {s.name: s.state for s in state.plan.steps} if state.plan else {}
        down = _servers_down(state)
        table = self.query_one("#triggers", DataTable)
        table.clear()
        for t in state.triggers:
            if t.command == "improve":
                provider = t.provider or "PROMPT_PROVIDER"
                profile = t.profile or (
                    "PROMPT_PRO_PROFILE" if t.tier == "pro" else "PROMPT_PROFILE"
                )
                tier = t.tier or "standard"
            else:
                provider = "prints PROMPT_PERSONA" if t.command == "persona" else "static snippet"
                profile = tier = "-"
            where = states.get(t.file, "unknown") if t.active else "commented out"
            # Once its file is there, what typing it needs beats the file's state (#220).
            deployed = where not in (deploy.MISSING, "unknown")
            if deployed and t.active and t.command == "improve" and t.provider in down:
                where = f"needs {PROVIDER_LABELS[t.provider]}"
            add_row(table, t.trigger, provider, tier, profile, where, t.file)
        target = self.query_one("#deploy-target", Static)
        if state.plan is None:
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
                # Deploy and detach then use the default folder: never silently (#115).
                text += f"\n{espanso.message}"
            target.update(text)

    @on(Button.Pressed, "#show-diff")
    def _diff(self) -> None:
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
        self.start_deploy()

    def start_deploy(self, target: DeployTarget | None = None) -> None:
        """Read the plan in a worker, then preview it in a dialog (also the previous install
        screen's step 3, with its ``target``)."""
        to = target or DeployTarget()
        if self.claim():
            self.report("Reading the match files…")
            self.background(lambda: self.app.call_from_thread(self._ask_deploy, to.read(), to))

    def claim(self) -> bool:
        """Start a deploy or detach unless one is already open: a second dialog would apply
        a plan the first one already changed."""
        if self.busy:
            self.report("A deploy or detach is already in progress; finish or cancel it first.")
            return False
        self.busy = True
        return True

    def _ask_deploy(self, plan: deploy.Plan, to: DeployTarget) -> None:
        if plan.is_noop:
            self.busy = False
            self.report("Nothing to do: every match file is in sync.")
            return
        if plan.only_forgets:  # the manifest only: no file is written, so nothing to ask
            self.report("Forgetting deploy records of files that are gone…")
            self.background(lambda: self._apply(plan, {}, to))
            return
        lines = [f"Into {plan.espanso_dir / 'match'}:"]
        lines += [f"{s.state:<9} {s.name}" for s in plan.steps]
        if plan.legacy is not None:
            lines.append(f"legacy    {plan.legacy.name} will be retired, with a backup")
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
            self.background(lambda: self._apply(plan, choices, to))

        self.app.push_screen(
            FormModal("Deploy the match files", fields, submit="Deploy", preview=preview), done
        )

    def _apply(
        self,
        plan: deploy.Plan,
        choices: dict[str, str],
        to: DeployTarget,
    ) -> None:
        """In the worker: apply the plan the person saw, unless the files changed since."""
        if plan_digest(to.read()) != plan_digest(plan):
            raise deploy.DeployError(
                "the match files or the deploy record changed since the preview; nothing was "
                "written. Press Deploy again to see the new plan"
            )
        outcome = deploy.apply(plan, choices)
        # The files are written: show them now, not after the restart, which can take a
        # while (#215). _done() reloads again once Espanso answered.
        self.app.call_from_thread(self.manage.reload)
        lines = list(outcome.lines)
        if outcome.changed and to.no_restart:
            lines.append("Espanso was not restarted (--no-restart).")
        elif outcome.changed and not deploy.restart_espanso():
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
            teach.equivalent("espanso", "deploy", *to.args),
        )

    def _done(self, message: str, error: bool, command: str | None = None) -> None:
        self.busy = False
        self.report(message, error=error, command=command)
        self.manage.reload()

    @on(Button.Pressed, "#detach")
    def _ask_detach_start(self) -> None:
        self.ask_detach()

    def ask_detach(self, *, remove_all: bool = False) -> None:
        """Read the deploy record in a worker, then ask (`espanso detach`); ``remove_all``
        picks that mode first."""
        if self.claim():
            self.background(
                lambda: self.app.call_from_thread(
                    self._ask_detach,
                    deploy.Manifest.load(),
                    deploy.espanso_dir().resolve(),
                    remove_all,
                )
            )

    def _ask_detach(self, manifest: deploy.Manifest, root: Path, remove_all: bool = False) -> None:
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
                value="remove-all" if remove_all else "keep-static",
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
