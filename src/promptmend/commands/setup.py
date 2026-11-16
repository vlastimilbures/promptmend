"""`setup`: the first-run flow. On a terminal it opens the wizard (tui/setup_wizard.py), its
options prefilling the steps. With --non-interactive (or --plain) it asks nothing:
settings and the default profile from the options, the API key from --api-key-stdin, a deploy
preview (applied with --deploy), and a smoke test against a stub on 127.0.0.1, never a paid
call."""

from __future__ import annotations

import typer

from .. import config, deploy, setup_flow, setup_guide, smoke
from ..factory import PROVIDER_NAMES
from ..redaction import safe_repr
from . import common
from . import ui as ui_cmd
from .common import guard
from .usage import disclosure

app = typer.Typer(name="setup", add_completion=False)


class _Steps:
    """Each step's outcome, for the summary; any failure makes setup exit 1."""

    def __init__(self) -> None:
        self.failed: list[str] = []
        self.todo: list[str] = []

    def heading(self, text: str) -> None:
        typer.echo("")
        typer.echo(common.paint(text, "cyan"))

    def fail(self, step: str, message: str) -> None:
        typer.echo(f"  failed: {message}", err=True)
        self.failed.append(step)

    def later(self, message: str) -> None:
        typer.echo(f"  {message}")
        self.todo.append(message)


def _settings_writable(steps: _Steps) -> bool:
    """Whether setup may write config.toml and the secrets file: not in legacy mode, where
    neither is read."""
    where = setup_flow.storage()
    if not where.writable:
        steps.later(
            f"{config.ENV_FILE_VAR} is set, so settings and keys stay in that file; setup "
            "does not write config.toml or the secrets file"
        )
        return False
    if where.unread:
        typer.echo(f"  {where.unread}")
    return True


def _save_choices(steps: _Steps, provider: str, profile: str) -> None:
    saved = setup_flow.save_choices(provider, profile, lambda line: typer.echo(f"  {line}"))
    if saved.error is not None:
        steps.fail("settings", saved.error)
    elif saved.created is not None:
        typer.echo(f"  Saved in {saved.created}")
    elif not saved.messages:
        typer.echo("  Unchanged.")


def _key_step(steps: _Steps, provider: str, *, writable: bool, from_stdin: bool) -> None:
    """With --api-key-stdin, the provider's key; else every key the triggers or the provider
    need (an Ollama default still leaves -i- on OpenRouter)."""
    if from_stdin:
        name = setup_flow.stdin_key(provider)
        if name is None:
            steps.fail("key", f"--api-key-stdin was given, but {provider} takes no key")
            return
        _one_key(steps, name, writable=writable, from_stdin=True)
        return
    rows = setup_flow.needed_keys(*common.load_layers())
    if not rows:
        typer.echo("  No key needed: every active trigger and your provider run locally.")
        return
    for row in rows:
        typer.echo(f"  {setup_guide.key_line(row)}")
    for row in rows:
        _one_key(steps, row.name, writable=writable, from_stdin=False)


def _one_key(steps: _Steps, name: str, *, writable: bool, from_stdin: bool) -> None:
    layers, _ = common.load_layers()
    entry = layers.entries[name]
    if not writable:
        if from_stdin:
            steps.fail("key", "the key was not saved: the settings cannot be written (see above)")
        return
    if not from_stdin:
        if not entry.value:
            steps.later(f"{name} is not set; `promptmend secrets set {name} --stdin` sets it")
        else:
            typer.echo(f"  {name} is set (from {common.source_label(entry.source)}).")
        return
    value = common.read_secret(name, from_stdin=True)
    try:
        setup_flow.save_key(name, value)
    except setup_flow.KEY_ERRORS as exc:
        steps.fail("key", str(exc))
        return
    typer.echo(f"  {name} saved in the secrets file.")


def _deploy_step(
    steps: _Steps,
    *,
    apply: bool | None,
    espanso_dir: str | None,
    launcher: str | None,
    no_restart: bool,
) -> None:
    from .espanso import make_plan, restart

    try:
        the_plan = make_plan(espanso_dir, launcher)
    except (deploy.DeployError, ValueError, OSError) as exc:
        steps.fail("deploy", str(exc))
        return
    width = max((len(step.name) for step in the_plan.steps), default=0)
    for name, words in setup_guide.file_rows(the_plan):
        typer.echo(f"  {name:<{width}}  {words}")
    if setup_flow.in_sync(the_plan):
        if the_plan.only_forgets:
            try:
                forgot = setup_flow.forget(the_plan)  # the manifest only: no file is written
            except (deploy.DeployError, OSError) as exc:
                steps.fail("deploy", str(exc))
                return
            for line in forgot.lines:
                typer.echo(f"  {line}")
        typer.echo("  Every match file is in sync.")
        return
    if not apply:
        typer.echo("  Dry run: nothing was written.")
        steps.later("deploy the match files: `promptmend espanso deploy`")
        return
    try:
        outcome = setup_flow.install(the_plan)  # an edited file is kept: no silent overwrite
    except (deploy.DeployError, OSError) as exc:
        steps.fail("deploy", str(exc))
        return
    for line in outcome.lines:
        typer.echo(f"  {line}")
    if outcome.changed:
        restart(no_restart)
    if outcome.kept:
        steps.later("files you edited were kept: `promptmend espanso deploy` to choose")


def _check_profile(name: str, overrides: tuple[str, ...]) -> None:
    try:
        setup_flow.check_profile(name, overrides)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None


@app.command(
    "setup", short_help="First run: settings, key, match files, and a smoke test on a local stub."
)
@guard
def setup(
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Ask nothing; use the options and current settings"
    ),
    provider: str | None = typer.Option(
        None, "--provider", help=f"PROMPT_PROVIDER: {', '.join(PROVIDER_NAMES)}"
    ),
    profile: str | None = typer.Option(None, "--profile", help="PROMPT_PROFILE"),
    api_key_stdin: bool = typer.Option(
        False, "--api-key-stdin", help="Read the provider's API key from the first line of stdin"
    ),
    deploy_files: bool | None = typer.Option(
        None,
        "--deploy/--no-deploy",
        help="--non-interactive: write the match files, or skip the step (default: a "
        "preview). On a terminal: focus Install (still a press), or leave the step out",
        show_default=False,
    ),
    espanso_dir: str | None = typer.Option(
        None, "--espanso-dir", help="Default: `espanso path config`"
    ),
    launcher: str | None = typer.Option(
        None, "--launcher", help="CLI path to write into the matches; default: this install's"
    ),
    no_restart: bool = typer.Option(False, "--no-restart", help="Do not restart Espanso"),
    smoke_test: bool = typer.Option(
        True, "--smoke-test/--no-smoke-test", help="Run improve against a local stub at the end"
    ),
    plain: bool = typer.Option(
        False,
        "--plain",
        help="The same as --non-interactive: print each step, ask nothing",
    ),
) -> None:
    """First run: settings, the API keys, the Espanso match files and a smoke test against a
    stub on 127.0.0.1 (never a paid call). On a terminal it opens full screen, one step at a
    time: --provider and --profile preselect a choice, and --no-deploy or --no-smoke-test skip a
    step. With --non-interactive (or --plain) it asks nothing. Re-run it any time; it changes
    only what you choose."""
    interactive = not (non_interactive or plain)
    if interactive and not ui_cmd.on_a_terminal():
        common.fail(
            "setup needs a terminal (stdin and stdout); run it in a terminal or pass "
            "--non-interactive",
            common.NEEDS_TERMINAL,
        )
    if api_key_stdin and interactive:
        raise typer.BadParameter("--api-key-stdin needs --non-interactive (stdin holds the key)")
    if provider is not None and provider not in PROVIDER_NAMES:
        raise typer.BadParameter(
            f"--provider must be one of {', '.join(PROVIDER_NAMES)}, got {safe_repr(provider)}"
        )
    for value, option in (
        (espanso_dir, "--espanso-dir"),
        (launcher, "--launcher"),
        (profile, "--profile"),
    ):
        common.no_key(value, option)
    if interactive:
        from ..tui.setup_wizard import SetupOptions

        if profile is not None:
            _check_profile(profile, common.load_layers()[1].profile_overrides)
        ui_cmd.open_setup(
            SetupOptions(
                espanso_dir,
                launcher,
                no_restart,
                standalone=True,
                provider=provider,
                profile=profile,
                deploy=deploy_files,
                smoke_test=smoke_test,
            )
        )
        return
    steps = _Steps()
    layers, settings = common.load_layers()
    common.show_findings(layers)
    typer.echo(
        "PromptMend setup: settings, API keys, Espanso's match files and a test on a local "
        "stub. Asks nothing: it uses the options and the current settings, and lists what "
        "is left with its command; re-run it any time."
    )

    steps.heading("Usage history")
    for line in disclosure(settings):
        typer.echo(f"  {line}")

    steps.heading("Settings")
    writable = _settings_writable(steps)
    if writable:
        layers, settings = common.load_layers()
    try:
        chosen = setup_flow.chosen_provider(provider, settings)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from None
    picked = profile if profile is not None else settings.profile
    _check_profile(picked, settings.profile_overrides)
    typer.echo(f"  Provider: {chosen}; profile: {picked}")
    if writable:
        _save_choices(steps, chosen, picked)
    elif (chosen, picked) != (settings.provider, settings.profile):
        steps.later(f"set PROMPT_PROVIDER={chosen} and PROMPT_PROFILE={picked} yourself")

    steps.heading("API keys")
    _key_step(steps, chosen, writable=writable, from_stdin=api_key_stdin)

    steps.heading("Espanso match files")
    if deploy_files is False:
        typer.echo("  Skipped (--no-deploy).")
    else:
        _deploy_step(
            steps,
            apply=deploy_files,
            espanso_dir=espanso_dir,
            launcher=launcher,
            no_restart=no_restart,
        )

    steps.heading("Smoke test (a stub on 127.0.0.1; no provider is called)")
    if smoke_test:
        result = smoke.run(chosen)
        if result.ok:
            typer.echo(f"  ok: {result.message}")
        else:
            steps.fail("smoke test", f"improve --provider {chosen}: {result.message}")
    else:
        typer.echo("  Skipped (--no-smoke-test).")

    steps.heading("Summary")
    for item in steps.todo:
        typer.echo(f"  to do: {item}")
    if steps.failed:
        common.fail(f"setup did not finish: {', '.join(steps.failed)} failed (see above)")
    if steps.todo:
        typer.echo(f"  Setup finished with {len(steps.todo)} thing(s) still to do (above).")
    else:
        typer.echo(f"  Setup finished. {setup_guide.NEXT_STEP}")
    typer.echo("  `promptmend doctor` checks everything again.")
