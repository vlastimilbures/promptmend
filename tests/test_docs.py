from __future__ import annotations

import importlib.util
import itertools
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

import pytest
from bench_module import bench

from promptmend.config import ENV_FILE_VAR, env_names

if TYPE_CHECKING:
    from typer._click import Command, Context

REPO = Path(__file__).resolve().parents[1]
# The user docs: an explicit list, so an untracked local file in docs/ is never checked, and
# docs/prompt-candidates/ (an archived decision record) stays out.
DOC_PAGES = (
    "README.md",
    "install.md",
    "usage.md",
    "commands.md",
    "configuration.md",
    "profiles.md",
    "privacy.md",
    "interface.md",
    "troubleshooting.md",
    "benchmark.md",
)
# README.md and every docs page: what the command and option checks read.
DOCS = ("README.md", *(f"docs/{page}" for page in DOC_PAGES))
GITHUB = "https://github.com/vlastimilbures/promptmend/blob/main/"
RAW = "https://raw.githubusercontent.com/vlastimilbures/promptmend/main/"


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _config_rows() -> set[str]:
    text = (REPO / "docs" / "configuration.md").read_text("utf-8")
    return set(re.findall(r"^\| `([A-Z_]+)`", text, re.MULTILINE))


# Every setting is documented in docs/configuration.md's settings tables and in .env.example
# (commented out with its default, except the key and persona), so a new variable cannot ship
# undocumented.
@pytest.mark.parametrize("name", env_names())
def test_setting_is_documented(name: str) -> None:
    assert name in _config_rows(), f"{name} missing from docs/configuration.md"
    example = (REPO / ".env.example").read_text("utf-8")
    assert re.search(rf"^#? ?{name}=", example, re.MULTILINE), f"{name} missing from .env.example"


# The settings tables document nothing Settings does not read (PROMPTMEND_ENV is read by the
# .env loader itself).
def test_docs_document_no_stale_settings() -> None:
    assert _config_rows() - {*env_names(), ENV_FILE_VAR} == set()


# Every bench output directory the docs mention, and the bench's own default, is gitignored, so
# following the docs cannot commit outputs (with --persona env they hold a private persona).
def test_documented_bench_outdirs_are_ignored() -> None:
    git = shutil.which("git")
    inside = git and subprocess.run([git, "rev-parse"], cwd=REPO, capture_output=True).returncode
    if git is None or inside != 0:
        pytest.skip("not a git checkout")
    docs = [REPO / "README.md", REPO / "CONTRIBUTING.md", *(REPO / "docs").rglob("*.md")]
    found = {m for p in docs for m in re.findall(r"--outdir[=\s]+(\S+)", p.read_text("utf-8"))}
    assert found, "the docs no longer suggest any --outdir; drop or adapt this test"
    for outdir in [*found, f"{bench.DEFAULT_OUTDIR}/20261004T120000000000Z"]:
        probe = f"{outdir.rstrip('/')}/results.json"
        ignored = subprocess.run([git, "check-ignore", "-q", probe], cwd=REPO, check=False)
        assert ignored.returncode == 0, outdir


# CONTRIBUTING's project tree names every module of the package (commands/ and tui/ are listed
# as folders), so a new module cannot be left out of the map.
def test_contributing_tree_lists_every_module() -> None:
    contributing = (REPO / "CONTRIBUTING.md").read_text("utf-8")
    tree = contributing[contributing.index("├── src/promptmend/") :]
    tree = tree[: tree.index("├── scripts/")]
    package = REPO / "src" / "promptmend"
    modules = [*package.glob("*.py"), *(package / "providers").glob("*.py")]
    missing = [p.name for p in modules if p.stem != "__init__" and f"── {p.name} " not in tree]
    assert missing == [], f"add {missing} to the project tree in CONTRIBUTING.md"


_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_INVOCATION = re.compile(r"(?<![\w/\\-])promptmend(?![\w:=-])([^`\n#]*)")
_FLAG = re.compile(r"(?<![\w-])(--?[a-zA-Z][\w-]*)")
# `promptmend config get NAME` / `set NAME VALUE`: the later spans are siblings.
_SIBLINGS = re.compile(r"`promptmend ([^`]+)`((?:\s*/\s*`[^`]+`)+)")
# Options the docs name for other tools: uv, the bench, the retired installer option.
_OTHER_TOOLS_FLAGS = {"--force", "--persona", "--suite", "--with-config"}
# A line or span that runs another tool on the package (`uv tool install promptmend -c …`,
# `brew upgrade promptmend`) names no promptmend command.
_OTHER_TOOL = re.compile(r"^\s*(?:uv|uvx|brew|winget|gh|pip)\s")


def _spans(text: str) -> list[str]:
    """Each inline code span outside code blocks, a line break in it read as a space."""
    return [s.replace("\n", " ") for s in re.findall(r"`([^`]+)`", _FENCE.sub("", text))]


def _documented_invocations(text: str) -> list[str]:
    """What follows `promptmend` in each code block line and inline code span (a
    comment cut off), e.g. ` history export [--format json|csv] [-o FILE]`, plus each
    `/ x` sibling span after one, under the first span's parent command."""
    lines = [line for b in _FENCE.findall(text) for line in b.splitlines()]
    spans = [s for s in [*lines, *_spans(text)] if not _OTHER_TOOL.match(s)]
    found = [m for span in spans for m in _INVOCATION.findall(span)]
    for first, rest in _SIBLINGS.findall(_FENCE.sub("", text).replace("\n", " ")):
        path = list(itertools.takewhile(lambda w: re.fullmatch(r"[a-z]+", w), first.split()))
        parent = " ".join(path[:-1])
        found += [f" {parent} {sibling}" for sibling in re.findall(r"`([^`]+)`", rest)]
    return found


# The Unreleased notes plus the newest release's, so the check never runs on an empty or
# command-free Unreleased alone (right after a release, or a first entry naming no command).
def _changelog_notes(changelog: str) -> str:
    sections = re.split(r"^## ", changelog, flags=re.MULTILINE)[1:]
    notes = []
    for section in sections:
        title, _, body = section.partition("\n")
        notes.append(body)
        if title.strip() != "Unreleased":
            return "".join(notes)
    raise AssertionError("CHANGELOG.md has no released version")


def test_changelog_notes_add_the_newest_release() -> None:
    released = "## 1.1.0 - 2026-10-02\n\n- `promptmend doctor`\n\n## 1.0.0 - 2026-10-01\n\n- y\n"
    for unreleased, expected in [
        ("## Unreleased\n\n", ["- `promptmend doctor`"]),
        ("", ["- `promptmend doctor`"]),
        ("## Unreleased\n\n- x\n\n", ["- x", "- `promptmend doctor`"]),
    ]:
        notes = _changelog_notes(f"# Changelog\n\n{unreleased}{released}")
        assert notes.split() == " ".join(expected).split()


def _cli() -> tuple[Command, Context]:
    import typer

    from promptmend import cli

    root = typer.main.get_command(cli.app)
    return root, root.make_context("promptmend", ["--help"], resilient_parsing=True)


# Commands a release removed (#275): the CHANGELOG's notes may still name them.
_REMOVED = ("config migrate", "config rollback", "profiles migrate")


# Every `promptmend <command> [<subcommand>] --flag` the README, the docs pages (and the
# CHANGELOG's newest notes) name exists in the CLI, so a renamed command or option cannot
# leave the docs behind. Walks the Click tree; the lazy commands load their modules, never
# textual.
@pytest.mark.parametrize("doc", [*DOCS, "CHANGELOG.md"])
def test_documented_commands_and_flags_exist(doc: str) -> None:
    import typer

    root, ctx = _cli()
    text = (REPO / doc).read_text("utf-8")
    invocations = _documented_invocations(text if doc != "CHANGELOG.md" else _changelog_notes(text))
    if doc in {"README.md", "CHANGELOG.md", "docs/commands.md"}:
        assert invocations, f"{doc} names no promptmend command; drop or adapt this test"
    for rest in invocations:
        if doc == "CHANGELOG.md" and rest.strip().startswith(_REMOVED):
            continue
        command, words = root, rest.split()
        while words and isinstance(command, typer.core.TyperGroup):
            word = words[0]
            if not re.fullmatch(r"[a-z]+", word):
                break
            sub = command.get_command(ctx, word)
            assert sub is not None, f"{doc}: `promptmend{rest}`: no command {word!r}"
            command, words = sub, words[1:]
        known = {"--help", *(o for p in command.params for o in (*p.opts, *p.secondary_opts))}
        for flag in _FLAG.findall(" ".join(words)):
            assert flag in known, f"{doc}: `promptmend{rest}`: no option {flag}"


# A code span that is only options (`--keep-static`, `--yes --stdin`) names
# options of some command: each must exist somewhere in the CLI. docs/benchmark.md documents
# the bench script's own options, so it is left out.
@pytest.mark.parametrize("doc", [d for d in DOCS if d != "docs/benchmark.md"])
def test_standalone_flags_exist(doc: str) -> None:
    import typer

    root, ctx = _cli()

    def options(command: Command) -> set[str]:
        names = {o for p in command.params for o in (*p.opts, *p.secondary_opts)}
        if isinstance(command, typer.core.TyperGroup):
            for name in command.list_commands(ctx):
                sub = command.get_command(ctx, name)
                assert sub is not None, name
                names |= options(sub)
        return names

    known = {"--help", *options(root), *_OTHER_TOOLS_FLAGS}
    spans = [s for s in _spans((REPO / doc).read_text("utf-8")) if s.startswith("--")]
    if doc in {"docs/commands.md", "docs/usage.md"}:
        assert spans, f"{doc} names no standalone option; drop or adapt this test"
    for span in spans:
        for flag in re.findall(r"(?<![\w-])(--[a-zA-Z][\w-]*)", span):
            assert flag in known, f"{doc}: `{span}`: no option {flag} in the CLI"


def test_readme_has_no_package_placeholders() -> None:
    readme = (REPO / "README.md").read_text("utf-8")
    for placeholder in ("<owner>/<tap>", "<bucket>", "<Publisher.Package>"):
        assert placeholder not in readme


# The release whose default prompt the benchmark's newest results score, pinned to the prompt's
# digest: a change to prompts/default.md fails here until both are updated (set the version to
# the release that ships the change) and docs/benchmark.md gets results for the new prompt.
_PROMPT_CHANGED_IN = (0, 19, 0)
_PROMPT_DIGEST = "c336e4851edba9c8b284726613bd7b3953ab317b3aca453396f0acc718bfa6e2"


def _result_headings() -> list[str]:
    text = (REPO / "docs" / "benchmark.md").read_text("utf-8")
    results = re.search(r"^## Results\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert results, "docs/benchmark.md has no '## Results' section"
    return re.findall(r"^### (.+)$", results.group(1), re.MULTILINE)


def test_benchmark_prompt_pin_is_current() -> None:
    import hashlib

    prompt = (REPO / "src" / "promptmend" / "prompts" / "default.md").read_bytes()
    assert hashlib.sha256(prompt).hexdigest() == _PROMPT_DIGEST, (
        "prompts/default.md changed: update _PROMPT_DIGEST and _PROMPT_CHANGED_IN, and add "
        "benchmark results for the new prompt to docs/benchmark.md"
    )


# Every results table names the prompt version it scored, newest first, and the newest scores
# the current prompt, so a table cannot pass for the current prompt's after a prompt change.
def test_benchmark_results_name_their_prompt_version() -> None:
    headings = _result_headings()
    assert headings, "docs/benchmark.md lists no results"
    versions = []
    for heading in headings:
        found = re.search(r"prompt of v(\d+)\.(\d+)\.(\d+)", heading, re.IGNORECASE)
        assert found, f"docs/benchmark.md: {heading!r} names no prompt version"
        versions.append(tuple(map(int, found.groups())))
    assert versions[0] >= _PROMPT_CHANGED_IN, f"{headings[0]!r} predates the current prompt"
    assert versions == sorted(versions, reverse=True), "results are not newest first"


def test_readme_benchmark_summary_links_the_details() -> None:
    readme = (REPO / "README.md").read_text("utf-8")
    section = re.search(r"^## Model benchmark\n(.*?)(?=^## )", readme, re.MULTILINE | re.DOTALL)
    assert section, "README lost its '## Model benchmark' section (#model-benchmark links)"
    assert f"]({GITHUB}docs/benchmark.md)" in section.group(1)


# Benchmark numbers live only in docs/benchmark.md (CONTRIBUTING's "one home" rule): scores,
# run counts, latencies, costs and percentages in CONTRIBUTING would go stale there unseen.
_BENCH_FIGURE = re.compile(
    r"(?<![\w/.-])\d+/\d+(?![\w/.-])|\b\d+ of \d+\b|\b\d+(?:\.\d+)? s\b|\$\d|\b\d+%|\b\d+ calls\b"
)


def test_contributing_holds_no_benchmark_figures() -> None:
    text = _FENCE.sub("", (REPO / "CONTRIBUTING.md").read_text("utf-8"))
    found = [m.group(0) for m in _BENCH_FIGURE.finditer(text)]
    assert found == [], f"move {found} from CONTRIBUTING.md to docs/benchmark.md and link it"


_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


def _links(text: str) -> list[str]:
    """Every Markdown link and image target outside code blocks."""
    return _LINK.findall(_FENCE.sub("", text))


# The only HTML tags README may use: PyPI's renderer keeps <details>/<summary> and <img>, and
# shows a <picture>'s <img> fallback.
_README_TAGS = {"details", "summary", "picture", "source", "img"}


# PyPI renders README.md as the project page, where a relative link or image is broken and
# GitHub-only syntax shows as raw text: every target is absolute, there is no mermaid or alert
# block, and only the HTML tags PyPI renders.
def test_readme_is_pypi_safe() -> None:
    readme = (REPO / "README.md").read_text("utf-8")
    relative = [t for t in _links(readme) if not t.startswith(("https://", "#"))]
    assert relative == [], f"README links must be absolute (PyPI): {relative}"
    assert re.search(r"<img\b[^>]*src=\"(?!https://)", readme) is None
    assert re.search(r"<source\b[^>]*srcset=\"(?!https://)", readme) is None
    for syntax in ("```mermaid", "> [!"):
        assert syntax not in readme, f"README uses {syntax!r}, which PyPI does not render"
    tags = set(re.findall(r"</?([a-zA-Z][\w-]*)[\s>/]", _FENCE.sub("", readme)))
    assert tags <= _README_TAGS, f"README uses HTML tags PyPI may drop: {tags - _README_TAGS}"


if TYPE_CHECKING:
    import render_diagram
else:
    render_diagram = _load_script("render_diagram")


# README's flow diagram is rendered from docs/flow.mmd: both SVGs carry the source's current
# hash, so an edited diagram fails here until scripts/render_diagram.py ran again.
def test_flow_diagram_svgs_match_the_source() -> None:
    assert render_diagram.SOURCE == REPO / "docs" / "flow.mmd"
    assert render_diagram.stale() == [], "docs/flow.mmd changed: run scripts/render_diagram.py"
    for svg in render_diagram.OUTPUTS.values():
        text = svg.read_text("utf-8")
        assert "<foreignObject" not in text, f"{svg.name}: labels must be SVG text (GitHub)"


def test_render_diagram_stamp_replaces_an_older_hash() -> None:
    old, new = "a" * 64, "b" * 64
    svg = render_diagram.stamp("<svg/>", old)
    assert render_diagram.stamped_digest(svg) == old
    assert render_diagram.stamp(svg, new) == f"<!-- flow.mmd sha256:{new} -->\n<svg/>"
    declared = render_diagram.stamp('<?xml version="1.0"?>\n<svg/>', new)
    assert declared.splitlines()[1] == f"<!-- flow.mmd sha256:{new} -->"
    assert render_diagram.stamped_digest("<svg/>") is None


# README's <picture> shows the dark SVG under a dark theme and the light one otherwise (PyPI
# shows the <img>), both by absolute raw URL.
def test_readme_picture_shows_the_flow_diagram() -> None:
    readme = (REPO / "README.md").read_text("utf-8")
    picture = re.search(r"<picture>(.*?)</picture>", readme, re.DOTALL)
    assert picture, "README lost its flow diagram"
    dark = re.search(r'<source media="\(prefers-color-scheme: dark\)" srcset="([^"]+)"', picture[1])
    light = re.search(r'<img alt="[^"]+" src="([^"]+)"', picture[1])
    assert dark
    assert dark[1] == f"{RAW}docs/flow-dark.svg"
    assert light
    assert light[1] == f"{RAW}docs/flow-light.svg"
    for url in (dark[1], light[1]):
        assert (REPO / url[len(RAW) :]).is_file(), url


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = _FENCE.sub("", path.read_text("utf-8"))
    return {_slug(h) for h in re.findall(r"^#{1,6} (.+)$", text, re.MULTILINE)}


def _target(doc: str, link: str) -> tuple[Path, str] | None:
    """The repository file and anchor a link points at, None for an outside link."""
    for base, root in ((GITHUB, REPO), (RAW, REPO)):
        if link.startswith(base):
            path, _, anchor = link[len(base) :].partition("#")
            return root / path, anchor
    if link.startswith(("http://", "https://", "mailto:")):
        return None
    path, _, anchor = link.partition("#")
    return ((REPO / doc).parent / path if path else REPO / doc).resolve(), anchor


# Every link from README.md, a docs page or a contributor page into the repository names a
# file that exists and, for a Markdown file, a heading it has, so a moved section cannot leave
# a dead link behind.
@pytest.mark.parametrize("doc", [*DOCS, "CONTRIBUTING.md", "CLAUDE.md", "SECURITY.md"])
def test_doc_links_resolve(doc: str) -> None:
    for link in _links((REPO / doc).read_text("utf-8")):
        target = _target(doc, link)
        if target is None:
            continue
        path, anchor = target
        assert path.exists(), f"{doc}: {link}: no such file"
        if anchor and path.suffix == ".md":
            assert anchor in _anchors(path), f"{doc}: {link}: no heading #{anchor}"


# The docs index (docs/README.md) and README's Documentation table list every page.
@pytest.mark.parametrize("index", ["README.md", "docs/README.md"])
def test_index_lists_every_docs_page(index: str) -> None:
    text = (REPO / index).read_text("utf-8")
    for page in DOC_PAGES[1:]:
        link = f"{GITHUB}docs/{page}" if index == "README.md" else page
        assert f"]({link})" in text, f"{index} does not link docs/{page}"


# Every docs page follows the same skeleton: a `# ` title first, then an intro, and a
# `## See also` at the end.
@pytest.mark.parametrize("page", DOC_PAGES)
def test_docs_page_skeleton(page: str) -> None:
    text = (REPO / "docs" / page).read_text("utf-8")
    assert text.startswith("# "), f"docs/{page} does not open with a '# ' title"
    assert not text.split("\n", 2)[1].strip(), f"docs/{page}: a blank line after the title"
    assert not text.split("\n", 3)[2].startswith("#"), f"docs/{page}: no intro paragraph"
    headings = re.findall(r"^## (.+)$", _FENCE.sub("", text), re.MULTILINE)
    assert headings[-1] == "See also", f"docs/{page} does not end with '## See also'"


# Every docs page in docs/ that is tracked is in DOC_PAGES, so a new page gets the checks.
def test_doc_pages_are_listed() -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not found")
    listed = subprocess.run(
        [git, "ls-files", "docs/*.md"], cwd=REPO, capture_output=True, text=True, check=False
    )
    if listed.returncode != 0:
        pytest.skip("not a git checkout")
    tracked = {Path(p).name for p in listed.stdout.split() if p.count("/") == 1}
    assert tracked <= set(DOC_PAGES), f"add {tracked - set(DOC_PAGES)} to DOC_PAGES"
