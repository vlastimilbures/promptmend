"""`setup`: the first-run flow. On a terminal it opens the wizard (tui/setup_wizard.py), its
options prefilling the steps. With --non-interactive (or the hidden alias --plain) it asks nothing:
cloud or local, the profile and the output from the options, the API key from
--api-key-stdin, a deploy preview (applied with --deploy), and a smoke test on the path of
the answer's triggers against a stub on 127.0.0.1, never a paid call. --provider is
deprecated (#390): it saves PROMPT_PROVIDER, which no trigger reads, and says so."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import typer

from .. import assets, config, deploy, settings_service, setup_flow, setup_guide, smoke
from ..config import OUTPUTS
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


def _save_choices(steps: _Steps, choices: Sequence[tuple[str, str]]) -> None:
    saved = setup_flow.save_choices(choices, lambda line: typer.echo(f"  {line}"))
    if saved.error is not None:
        steps.fail("settings", saved.error)
    elif saved.created is not None:
        typer.echo(f"  Saved in {saved.created}")
    elif not saved.messages:
        typer.echo("  Unchanged.")


def _key_step(
    steps: _Steps, use: str, provider: str | None, *, writable: bool, from_stdin: bool
) -> None:
    """With --api-key-stdin, the cloud key the answer ``use`` needs (the deprecated
    --provider's, when given); else every key the triggers or the provider need, unless
    PROMPT_LOCAL_ONLY refuses them."""
    layers, settings = settings_service.load_layers()
    if from_stdin:
        source = provider if provider is not None else setup_flow.key_provider(use, settings)
        name = setup_flow.stdin_key(source) if source is not None else None
        if name is None:
            steps.fail("key", f"--api-key-stdin was given, but {provider or use} takes no key")
            return
        _one_key(steps, name, writable=writable, from_stdin=True)
        return
    rows = setup_flow.needed_keys(layers, settings, use)
    if not rows:
        # Local where it cannot be saved (legacy mode) and is not set yet: not needed once it is.
        pending = use == setup_guide.LOCAL and not writable and not settings.local_only
        answered = setup_guide.as_answered(settings, use)
        typer.echo(f"  {setup_guide.no_key_line(answered, pending=pending)}")
        return
    for row in rows:
        typer.echo(f"  {setup_guide.key_line(row)}")
    for row in rows:
        _one_key(steps, row.name, writable=writable, from_stdin=False)


def _one_key(steps: _Steps, name: str, *, writable: bool, from_stdin: bool) -> None:
    layers, _ = settings_service.load_layers()
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


def _one_of(value: str | None, option: str, allowed: Sequence[str]) -> None:
    if value is not None and value not in allowed:
        raise typer.BadParameter(
            f"{option} must be one of {', '.join(allowed)}, got {safe_repr(value)}"
        )


def _smoke_step(steps: _Steps, use: str) -> None:
    """One run per path of the answer's triggers (setup_guide.smoke_runs())."""
    settings = settings_service.load_layers()[1]
    for run in setup_guide.smoke_runs(use, assets.triggers(), settings):
        if run.provider is None:  # local, but no trigger stays on this computer
            steps.later(setup_guide.smoke_result(run, False, ""))
            continue
        result = smoke.run(run.provider)
        line = setup_guide.smoke_result(run, result.ok, result.message)
        if result.ok:
            typer.echo(f"  ok: {line}")
        else:
            steps.fail("smoke test", line)


@app.command(
    "setup", short_help="First run: settings, key, match files, and a smoke test on a local stub."
)
@guard
def setup(
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Ask nothing; use the options and current settings"
    ),
    use: str | None = typer.Option(
        None,
        "--use",
        help=f"Which triggers you use: {', '.join(setup_guide.USES)} (local saves "
        "PROMPT_LOCAL_ONLY=true; cloud and both remove it)",
    ),
    profile: str | None = typer.Option(None, "--profile", help="PROMPT_PROFILE"),
    output: str | None = typer.Option(
        None, "--output", help=f"PROMPT_OUTPUT: {', '.join(OUTPUTS)}"
    ),
    provider: str | None = typer.Option(
        None,
        "--provider",
        help="Deprecated, will be removed in a later release (use --use): saves "
        f"PROMPT_PROVIDER ({', '.join(PROVIDER_NAMES)}), which no trigger reads; on a terminal "
        "it is not saved",
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Read the OpenRouter key (cloud or both; the --provider's key with --provider) "
        "from the first line of stdin",
    ),
    deploy_files: bool | None = typer.Option(
        None,
        "--deploy/--no-deploy",
        help="--non-interactive: write the match files, or skip the step (default: a "
        "preview). On a terminal: focus Deploy (still a press), or leave the step out",
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
    # An old alias of --non-interactive, hidden since #405 and still accepted (no removal date).
    plain: bool = typer.Option(False, "--plain", hidden=True),
) -> None:
    """First run: cloud or local, the API keys, the profile, paste or clipboard, the Espanso
    match files and a smoke test against a stub on 127.0.0.1 (never a paid call). On a
    terminal it opens full screen, one step at a time: --use, --profile and --output preselect
    a choice, and --no-deploy or --no-smoke-test skip a step. With --non-interactive it asks
    nothing. Re-run it any time; it changes only what you choose."""
    interactive = not (non_interactive or plain)
    if interactive and not ui_cmd.on_a_terminal():
        common.fail(
            "setup needs a terminal (stdin and stdout); run it in a terminal or pass "
            "--non-interactive",
            common.NEEDS_TERMINAL,
        )
    if api_key_stdin and interactive:
        raise typer.BadParameter("--api-key-stdin needs --non-interactive (stdin holds the key)")
    _one_of(use, "--use", setup_guide.USES)
    _one_of(output, "--output", OUTPUTS)
    _one_of(provider, "--provider", PROVIDER_NAMES)
    for value, option in (
        (espanso_dir, "--espanso-dir"),
        (launcher, "--launcher"),
        (profile, "--profile"),
    ):
        common.no_key(value, option)
    if interactive:
        from ..tui.setup_wizard import SetupOptions

        if profile is not None:
            _check_profile(profile, settings_service.load_layers()[1].profile_overrides)
        ui_cmd.open_setup(
            SetupOptions(
                espanso_dir,
                launcher,
                no_restart,
                standalone=True,
                # The deprecated --provider is not saved here; it only preselects an answer.
                use=use
                if use is not None or provider is None
                else (setup_flow.use_for_provider(provider)),
                profile=profile,
                output=output,
                deploy=deploy_files,
                smoke_test=smoke_test,
                provider=provider,
            )
        )
        return
    steps = _Steps()
    layers, settings = settings_service.load_layers()
    common.show_findings(layers)
    typer.echo(
        "PromptMend setup: cloud or local, API keys, the profile and the output, Espanso's "
        "match files and a test on a local stub. Asks nothing: it uses the options and the "
        "current settings, and lists what is left with its command; re-run it any time."
    )

    steps.heading("Usage history")
    for line in disclosure(settings):
        typer.echo(f"  {line}")

    steps.heading("Settings")
    writable = _settings_writable(steps)
    if writable:
        layers, settings = settings_service.load_layers()
    # An option not given keeps what config.toml holds (saved_settings()): a value only the
    # shell gives is never written, nor a saved one removed, without being chosen.
    saved = setup_flow.saved_settings(settings)
    picked = profile if profile is not None else saved.profile
    _check_profile(picked, settings.profile_overrides)
    # Without --use nothing about PROMPT_LOCAL_ONLY changes: the answer is the saved one.
    answer = use if use is not None else setup_flow.current_use(saved)
    out = output if output is not None else saved.output
    over = setup_flow.env_overrides(layers, replace(saved, profile=picked, output=out))
    typer.echo(
        f"  Triggers: {answer}; "
        f"profile: {setup_guide.shown_choice('PROMPT_PROFILE', picked, over)}; "
        f"output: {setup_guide.shown_choice('PROMPT_OUTPUT', out, over)}"
    )
    legacy = setup_flow.storage().legacy
    if answer == setup_guide.LOCAL:
        # What is true here: saved now (--use local), set already (config.toml, or in legacy
        # mode the .env, holds it), or for the person to add (legacy mode).
        note = setup_guide.local_note(
            assets.triggers(), settings, legacy=legacy, already=saved.local_only, headless=True
        )
        typer.echo(f"  {note}")
    if provider is not None:
        typer.echo(f"  {setup_guide.PROVIDER_DEPRECATED}")
    choices = [("PROMPT_PROFILE", picked), ("PROMPT_OUTPUT", out)]
    if use is not None:
        choices.insert(0, (setup_flow.LOCAL_ONLY, setup_flow.local_only_value(use)))
    if provider is not None:
        choices.append(("PROMPT_PROVIDER", provider))
    if writable:
        _save_choices(steps, choices)
    else:
        fixes = [
            f"remove {name} from {legacy}"
            if value == setup_flow.DEFAULTS[name]
            else f"add {name}={value} to {legacy}"
            for name, value in choices
            if value != setup_flow.choice_value(name, settings)
        ]
        if fixes:
            steps.later("; ".join(fixes))
    if writable:
        problem = setup_flow.use_problem(answer, *settings_service.load_layers())
        if problem:
            steps.later(problem)

    steps.heading("API keys")
    _key_step(steps, answer, provider, writable=writable, from_stdin=api_key_stdin)

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
        _smoke_step(steps, answer)
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
        settings = setup_flow.saved_settings(settings_service.load_layers()[1])
        typer.echo(
            f"  Setup finished. {setup_guide.next_step(answer, assets.triggers(), settings)}"
        )
    typer.echo("  `promptmend doctor` checks everything again.")
