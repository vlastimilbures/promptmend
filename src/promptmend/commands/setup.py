"""`setup`: the first-run flow. On a terminal it opens the wizard (tui/setup_wizard.py), its
options prefilling the steps (#244). With --non-interactive (or --plain) it asks nothing:
settings and the default profile from the options, the API key from --api-key-stdin, a deploy
preview (applied with --deploy), and a smoke test against a stub on 127.0.0.1, never a paid
call."""

from __future__ import annotations

import typer

from .. import assets, config, config_files, config_store, deploy, setup_guide, smoke
from ..config import setting_fields
from ..factory import PROVIDER_KEYS, PROVIDER_NAMES
from ..prompt_builder import system_prompt
from ..redaction import safe_repr
from . import common
from . import settings as settings_cmd
from . import ui as ui_cmd
from .common import guard
from .usage import disclosure

app = typer.Typer(name="setup", add_completion=False)

_DEFAULTS = {f.metadata["env"]: f.metadata["default"] for f in setting_fields()}


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
    """Whether setup may write config.toml and the secret store: not in legacy mode, where
    neither is read."""
    if common.legacy_env():
        steps.later(
            f"{config.ENV_FILE_VAR} is set, so settings and keys stay in that file; setup "
            "does not write config.toml or the secret store"
        )
        return False
    if unread := setup_guide.unread_env_note(config.unread_env_files()):
        typer.echo(f"  {unread}")
    return True


# The setting each choice is, and its Settings field.
_CHOICES = {"PROMPT_PROVIDER": "provider", "PROMPT_PROFILE": "profile"}


def current_choice(name: str) -> str:
    """The effective value of PROMPT_PROVIDER or PROMPT_PROFILE."""
    return str(getattr(common.load_layers()[1], _CHOICES[name]))


def choice_command(name: str, value: str) -> list[str]:
    """The `config` command save_choice() runs for ``value``: a default is unset."""
    return ["unset", name] if value == _DEFAULTS[name] else ["set", name, value]


def save_choice(name: str, value: str) -> tuple[str | None, list[str]]:
    """Save PROMPT_PROVIDER or PROMPT_PROFILE as setup does, shared with the wizard: an
    unchanged value writes nothing (None, no command), and a default is not pinned but
    removed from config.toml, so a later change of the default still applies. Else what was
    done, with the `config` command that did it (choice_command(), or `set` when removing
    the default did not make it apply), for the wizard's log."""
    if current_choice(name) == value:
        return None, []
    if value == _DEFAULTS[name]:
        removed, path = settings_cmd.unset_setting(name)
        if removed and current_choice(name) == value:
            return f"{name} removed from {path}; its default, {value}, applies", ["unset", name]
    saved = settings_cmd.save_setting(name, value)
    notes = settings_cmd.after_save(name)
    return "; ".join([f"{name}={value} saved in {saved.path}", *notes]), ["set", name, value]


def _save_choices(steps: _Steps, provider: str, profile: str) -> None:
    changed = False
    for name, value in (("PROMPT_PROVIDER", provider), ("PROMPT_PROFILE", profile)):
        try:
            message, _ = save_choice(name, value)
        except (ValueError, common.CommandError, config_store.ConfigStoreError) as exc:
            steps.fail("settings", str(exc))
            return
        if message:
            typer.echo(f"  {message}")
            changed = True
    if changed:
        return
    snapshot = config_store.read_settings()
    if config_files.is_file(snapshot.path):
        typer.echo("  Unchanged.")
        return
    # Saved mode from here on, even with every default: config.toml is where settings go.
    try:
        saved = config_store.save_settings(snapshot, {})
    except (ValueError, config_store.ConfigStoreError) as exc:
        steps.fail("settings", str(exc))
        return
    typer.echo(f"  Saved in {saved.path}")


def _key_step(steps: _Steps, provider: str, *, writable: bool, from_stdin: bool) -> None:
    """With --api-key-stdin, the provider's key; else every key the triggers or the provider
    need (an Ollama default still leaves -i- on OpenRouter)."""
    if from_stdin:
        name = PROVIDER_KEYS.get(provider)
        if name is None:
            steps.fail("key", f"--api-key-stdin was given, but {provider} takes no key")
            return
        _one_key(steps, name, writable=writable, from_stdin=True)
        return
    layers, settings = common.load_layers()
    rows = [r for r in setup_guide.key_rows(layers, settings, assets.triggers()) if r.needed]
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
        settings_cmd.store_key(name, value)
    except (common.CommandError, ValueError, config_files.SecretStoreError) as exc:
        steps.fail("key", str(exc))
        return
    typer.echo(f"  {name} saved in the secret store.")


def _deploy_step(
    steps: _Steps,
    *,
    apply: bool | None,
    espanso_dir: str | None,
    launcher: str | None,
    no_restart: bool,
) -> None:
    from ..cli import _make_plan, _restart

    try:
        the_plan = _make_plan(espanso_dir, launcher)
    except (deploy.DeployError, ValueError, OSError) as exc:
        steps.fail("deploy", str(exc))
        return
    width = max((len(step.name) for step in the_plan.steps), default=0)
    for name, words in setup_guide.file_rows(the_plan):
        typer.echo(f"  {name:<{width}}  {words}")
    if the_plan.is_noop or the_plan.only_forgets:
        if the_plan.only_forgets:
            try:
                forgot = deploy.apply(the_plan, {})  # the manifest only: no file is written
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
        outcome = deploy.apply(the_plan, {})  # an edited file is kept: no silent overwrite
    except (deploy.DeployError, OSError) as exc:
        steps.fail("deploy", str(exc))
        return
    for line in outcome.lines:
        typer.echo(f"  {line}")
    if outcome.changed:
        _restart(no_restart)
    if outcome.kept:
        steps.later("files you edited were kept: `promptmend espanso deploy` to choose")


def _check_profile(name: str, overrides: tuple[str, ...]) -> None:
    try:
        system_prompt(name, "", overrides)
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
    chosen = provider if provider is not None else settings.provider
    if chosen not in PROVIDER_NAMES:
        raise typer.BadParameter(
            f"PROMPT_PROVIDER {safe_repr(chosen)} is not a provider; pass --provider"
        )
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
