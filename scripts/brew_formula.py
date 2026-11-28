"""Generate the Homebrew formula for the vlastimilbures/homebrew-tap repository (#95).

    python3 scripts/brew_formula.py --sdist dist/promptmend-X.Y.Z.tar.gz \\
        -o ../homebrew-tap/Formula/promptmend.rb

Run it from a checkout of the release tag, so uv.lock and pyproject.toml are the released
ones. The formula installs the released sdist (its URL on the GitHub Release, its SHA-256
taken from the local file, which `gh attestation verify` can check first) into a virtualenv
with `Language::Python::Virtualenv`, and every runtime dependency as a `resource` pinned to
the exact sdist uv.lock records (URL and SHA-256), like homebrew-pypi-poet but from the lock,
so `brew install` gets the versions the release was tested with. Dependencies whose markers
hold only on macOS or only on Linux go into `on_macos`/`on_linux`; Windows-only ones (colorama)
are left out. A marker this script cannot evaluate stops it rather than guess. Its caveats,
which `brew install` prints at the end, name the next steps: start Espanso, then run
`promptmend setup` (CAVEATS).

Standard library only, and offline: it reads files and never downloads anything.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT = "promptmend"
FORMULA = "promptmend"  # the tap's Formula/<FORMULA>.rb; the class name follows from it
COMMAND = "promptmend"  # the console script the formula's test runs
REPO_URL = "https://github.com/vlastimilbures/promptmend"
DESC = "Rewrite a rough draft into a precise LLM prompt from an Espanso trigger"
# Homebrew's Python the virtualenv is built on; the markers are evaluated for this version.
DEFAULT_PYTHON = "3.14"
ROOT = Path(__file__).resolve().parents[1]
# The formula's caveats, one line each: what to do after `brew install` (#402).
CAVEATS = (
    "PromptMend's triggers run in Espanso (https://espanso.org/install/).",
    "1. Start Espanso: open the Espanso app, or run `espanso start`.",
    f"2. Run `{COMMAND} setup`: the settings, an API key, a test call and the",
    "   deploy of the Espanso match files.",
)


def formula_class(name: str) -> str:
    """The Ruby class Homebrew expects in ``Formula/<name>.rb``, as its ``Formulary`` derives it.

    ``promptmend`` gives ``Promptmend``; a ``-``, ``_`` or ``.`` starts a new capital
    (``prompt-workflow`` gives ``PromptWorkflow``) and ``+`` becomes ``x``.
    """
    head = name[:1].upper() + name[1:].lower()
    return re.sub(r"[-_.\s]([a-zA-Z0-9])", lambda m: m.group(1).upper(), head).replace("+", "x")


class FormulaError(Exception):
    """The lock or the sdist cannot give a formula; the message says why."""


def normalize(name: str) -> str:
    """PEP 503: `Foo_Bar.baz` and `foo-bar-baz` are the same package."""
    return re.sub(r"[-_.]+", "-", name).lower()


# --- Markers -------------------------------------------------------------------------------

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'[^']*'|\"[^\"]*\")|(?P<op>==|!=|<=|>=|<|>|\(|\))"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_.]*))"
)
_VERSION_VARS = {"python_version", "python_full_version"}


def _tokens(marker: str) -> list[str]:
    tokens, pos = [], 0
    while pos < len(marker.rstrip()):
        match = _TOKEN.match(marker, pos)
        if not match:
            raise FormulaError(f"cannot read the marker {marker!r}")
        tokens.append(match.group(match.lastgroup or "word"))
        pos = match.end()
    return tokens


def _version(text: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(\.\d+)*", text):
        raise FormulaError(f"cannot compare the version {text!r}")
    return tuple(int(part) for part in text.split("."))


def _compare(left: str, op: str, right: str, versions: bool) -> bool:
    if versions:
        a, b = _version(left), _version(right)
        width = max(len(a), len(b))
        a, b = a + (0,) * (width - len(a)), b + (0,) * (width - len(b))
        ordered = {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}
        if op in ordered:
            return ordered[op]
        return (a == b) == (op == "==")
    if op in ("==", "!="):
        return (left == right) == (op == "==")
    raise FormulaError(f"cannot compare {left!r} {op} {right!r}")


def evaluate(marker: str, env: Mapping[str, str]) -> bool:
    """A PEP 508 marker (`and`, `or`, parentheses, comparisons) for one environment. A
    variable missing from ``env`` stops the script; `extra` is never set (no extras are
    installed), so `extra == '...'` is false."""
    tokens = _tokens(marker)
    pos = 0

    def take() -> str:
        nonlocal pos
        if pos >= len(tokens):
            raise FormulaError(f"the marker {marker!r} ends too early")
        pos += 1
        return tokens[pos - 1]

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def value(token: str) -> tuple[str, str | None]:
        if token[0] in "'\"":
            return token[1:-1], None
        if token == "extra":  # noqa: S105 - a marker variable, not a password
            return "", token
        if token not in env:
            raise FormulaError(
                f"the marker {marker!r} uses {token}, which this script does not set"
            )
        return env[token], token

    def atom() -> bool:
        if peek() == "(":
            take()
            result = disjunction()
            if take() != ")":
                raise FormulaError(f"unbalanced parentheses in {marker!r}")
            return result
        left, left_var = value(take())
        op = take()
        if op == "not" and peek() == "in":
            op += " " + take()
        right, right_var = value(take())
        if "extra" in (left_var, right_var):
            return False
        if op in ("in", "not in"):
            return (left in right) == (op == "in")
        versions = bool({left_var, right_var} & _VERSION_VARS)
        return _compare(left, op, right, versions)

    def conjunction() -> bool:
        result = atom()
        while peek() == "and":
            take()
            result = atom() and result
        return result

    def disjunction() -> bool:
        result = conjunction()
        while peek() == "or":
            take()
            result = conjunction() or result
        return result

    result = disjunction()
    if pos != len(tokens):
        raise FormulaError(f"cannot read the marker {marker!r}")
    return result


def environments(python: str) -> dict[str, dict[str, str]]:
    """The two platforms Homebrew installs on, with Homebrew's CPython ``python``."""
    if not re.fullmatch(r"3\.\d+", python):
        raise FormulaError(f"--python must look like 3.14, not {python!r}")
    common = {
        "python_version": python,
        "python_full_version": f"{python}.0",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "os_name": "posix",
    }
    return {
        "macos": {**common, "sys_platform": "darwin", "platform_system": "Darwin"},
        "linux": {**common, "sys_platform": "linux", "platform_system": "Linux"},
    }


# --- Lock ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Resource:
    name: str
    version: str
    url: str
    sha256: str


def _index(lock: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    packages: dict[str, list[Mapping[str, Any]]] = {}
    for package in lock.get("package", []):
        packages.setdefault(normalize(str(package["name"])), []).append(package)
    return packages


def _find(
    packages: Mapping[str, list[Mapping[str, Any]]], dep: Mapping[str, Any]
) -> Mapping[str, Any]:
    name = normalize(str(dep["name"]))
    found = [
        p
        for p in packages.get(name, [])
        if "version" not in dep or str(p.get("version")) == str(dep["version"])
    ]
    if len(found) != 1:
        what = "is not in uv.lock" if not found else "has several versions in uv.lock"
        raise FormulaError(f"{name} {what}; cannot pick its resource")
    return found[0]


def runtime_packages(lock: Mapping[str, Any], env: Mapping[str, str]) -> set[tuple[str, str]]:
    """Every package (name, version) the project needs at run time in ``env``: its
    `dependencies` (never the dev group) and theirs, each edge's marker evaluated, extras
    followed."""
    packages = _index(lock)
    project = _find(packages, {"name": PROJECT})
    needed: set[tuple[str, str]] = set()
    todo: list[tuple[Mapping[str, Any], tuple[str, ...]]] = [(project, ())]
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    while todo:
        package, extras = todo.pop()
        key = (normalize(str(package["name"])), str(package.get("version")), extras)
        if key in seen:
            continue
        seen.add(key)
        deps: list[Mapping[str, Any]] = list(package.get("dependencies", []))
        optional = package.get("optional-dependencies", {})
        for extra in extras:
            deps += optional.get(extra, [])
        for dep in deps:
            if "marker" in dep and not evaluate(str(dep["marker"]), env):
                continue
            child = _find(packages, dep)
            needed.add((normalize(str(child["name"])), str(child.get("version"))))
            todo.append((child, tuple(sorted(dep.get("extra", [])))))
    return {item for item in needed if item[0] != PROJECT}


def resource(lock: Mapping[str, Any], name: str, version: str) -> Resource:
    package = _find(_index(lock), {"name": name, "version": version})
    sdist = package.get("sdist")
    if not isinstance(sdist, Mapping) or "url" not in sdist or "hash" not in sdist:
        raise FormulaError(f"{name} has no sdist in uv.lock; Homebrew builds from sdists")
    algorithm, _, digest = str(sdist["hash"]).partition(":")
    if algorithm != "sha256" or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise FormulaError(f"{name}'s sdist has no SHA-256 in uv.lock")
    url = str(sdist["url"])
    if not url.startswith("https://"):
        raise FormulaError(f"{name}'s sdist URL is not https: {url}")
    return Resource(name, str(package["version"]), url, digest)


# --- Formula -------------------------------------------------------------------------------


def _resource_block(item: Resource, indent: str) -> list[str]:
    return [
        f'{indent}resource "{item.name}" do',
        f'{indent}  url "{item.url}"',
        f'{indent}  sha256 "{item.sha256}"',
        f"{indent}end",
    ]


def _blocks(items: Iterable[Resource], indent: str) -> list[str]:
    lines: list[str] = []
    for item in sorted(items, key=lambda r: r.name):
        lines += [*_resource_block(item, indent), ""]
    return lines


def render(
    lock: Mapping[str, Any], *, version: str, sdist_url: str, sdist_sha256: str, python: str
) -> str:
    """The formula text: the same lock and arguments always give the same bytes."""
    if not re.fullmatch(r"[0-9a-f]{64}", sdist_sha256):
        raise FormulaError("the sdist SHA-256 must be 64 lower-case hex digits")
    project = _find(_index(lock), {"name": PROJECT})
    if str(project.get("version")) != version:
        raise FormulaError(f"uv.lock is for {project.get('version')}, not {version}: run uv lock")
    per_env = {name: runtime_packages(lock, env) for name, env in environments(python).items()}
    both = per_env["macos"] & per_env["linux"]
    lines = [
        f"# Generated by scripts/brew_formula.py from {PROJECT} {version} and its uv.lock;",
        "# regenerate it for each release rather than editing it by hand.",
        f"class {formula_class(FORMULA)} < Formula",
        "  include Language::Python::Virtualenv",
        "",
        f'  desc "{DESC}"',
        f'  homepage "{REPO_URL}"',
        f'  url "{sdist_url}"',
        f'  sha256 "{sdist_sha256}"',
        '  license "MIT"',
        "",
        f'  depends_on "python@{python}"',
        "",
        *_blocks((resource(lock, *item) for item in both), "  "),
    ]
    for platform, block in (("macos", "on_macos"), ("linux", "on_linux")):
        only = per_env[platform] - both
        if only:
            body = _blocks((resource(lock, *item) for item in only), "    ")
            lines += [f"  {block} do", *body[:-1], "  end", ""]
    lines += [
        "  def install",
        "    virtualenv_install_with_resources",
        "  end",
        "",
        # Shown after `brew install` (#402): the CLI alone does nothing until setup.
        "  def caveats",
        "    <<~EOS",
        *(f"      {line}" for line in CAVEATS),
        "    EOS",
        "  end",
        "",
        "  test do",
        f'    assert_equal version.to_s, shell_output("#{{bin}}/{COMMAND} --version").strip',
        "  end",
        "end",
    ]
    return "\n".join(lines) + "\n"


def sdist_url(version: str) -> str:
    return f"{REPO_URL}/releases/download/v{version}/promptmend-{version}.tar.gz"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sdist", type=Path, help="the released sdist, downloaded")
    source.add_argument("--sha256", help="the released sdist's SHA-256, if not downloaded")
    parser.add_argument("--lock", type=Path, default=ROOT / "uv.lock")
    parser.add_argument("--pyproject", type=Path, default=ROOT / "pyproject.toml")
    parser.add_argument("--python", default=DEFAULT_PYTHON, help="Homebrew's python@X.Y")
    parser.add_argument("-o", "--output", type=Path, help="write here instead of stdout")
    args = parser.parse_args(argv)
    try:
        pyproject = tomllib.loads(args.pyproject.read_text(encoding="utf-8"))
        version = str(pyproject["project"]["version"])
        if args.sdist is not None:
            expected = f"promptmend-{version}.tar.gz"
            if args.sdist.name != expected:
                raise FormulaError(f"{args.sdist.name} is not {expected} (check out v{version})")
            digest = sha256_of(args.sdist)
        else:
            digest = args.sha256
        lock = tomllib.loads(args.lock.read_text(encoding="utf-8"))
        text = render(
            lock,
            version=version,
            sdist_url=sdist_url(version),
            sdist_sha256=digest,
            python=args.python,
        )
    except (FormulaError, OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        print(f"brew_formula: {exc}", file=sys.stderr)
        return 1
    if args.output is None:
        sys.stdout.write(text)
    else:
        args.output.write_text(text, encoding="utf-8", newline="\n")
        print(f"Wrote {args.output} for {PROJECT} {version}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
