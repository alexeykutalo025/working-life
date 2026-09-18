"""The front end, checked without a browser.

There is no JavaScript engine in the test environment, so these tests check the
things that can be checked from the text - and they exist because each one
corresponds to a failure that actually happened while building this, or to a
promise the specification makes that is easy to break silently.

The most important is the unterminated-string check. A real newline inside a
quoted string is a syntax error that takes the whole module down, and a broken
module shows the user an error box where a screen should be. It bit this build
twice.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "recall" / "web"
JS_FILES = sorted(WEB.glob("static/js/**/*.js"))
CSS = WEB / "static" / "app.css"
INDEX = WEB / "index.html"


def test_there_are_front_end_files_to_check():
    assert JS_FILES, "no JavaScript found; the glob is wrong"
    assert CSS.exists() and INDEX.exists()


# ---------------------------------------------------------------------------
# Syntax
# ---------------------------------------------------------------------------


def unterminated_strings(source: str) -> list[int]:
    """Line numbers where a ' or " string is left open at end of line.

    A small state machine rather than a regex, so it is not fooled by
    apostrophes in prose, quotes inside comments, or template literals - all of
    which a regex gets wrong and then reports fifty false positives.
    """
    problems: list[int] = []
    line = 1
    i = 0
    n = len(source)

    while i < n:
        ch = source[i]

        if ch == "\n":
            line += 1
            i += 1
            continue

        # Comments: skip them entirely.
        if ch == "/" and i + 1 < n:
            if source[i + 1] == "/":
                while i < n and source[i] != "\n":
                    i += 1
                continue
            if source[i + 1] == "*":
                i += 2
                while i + 1 < n and not (source[i] == "*" and source[i + 1] == "/"):
                    if source[i] == "\n":
                        line += 1
                    i += 1
                i += 2
                continue

        # Template literals may legitimately span lines.
        if ch == "`":
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == "\n":
                    line += 1
                if source[i] == "`":
                    break
                i += 1
            i += 1
            continue

        # A quoted string must close on the line it opened.
        if ch in "'\"":
            quote = ch
            start_line = line
            i += 1
            closed = False
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == "\n":
                    break          # reached end of line without closing
                if source[i] == quote:
                    closed = True
                    break
                i += 1
            if closed:
                i += 1             # step over the closing quote
            else:
                problems.append(start_line)
                # Leave the newline for the outer loop to count, or the rest of
                # the file is reported against the wrong lines.
            continue

        i += 1

    return problems


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_no_unterminated_string_literals(path: Path):
    """A newline inside a quoted string takes the whole module down."""
    problems = unterminated_strings(path.read_text(encoding="utf-8"))
    assert problems == [], (
        f"{path.name} has a string left open at line(s) {problems}. "
        "The module will not load and the screen will show an error box."
    )


def test_the_checker_finds_a_real_one():
    """Guard against the check passing because it checks nothing.

    One stray newline throws off every quote after it, so the report cascades -
    which is what the browser does too. What matters is that the first line
    named is the line that actually broke.
    """
    broken = "const a = 'open\nstill open';"
    assert unterminated_strings(broken)[0] == 1


def test_the_checker_reports_the_line_that_broke():
    """The line number has to survive the lines before it."""
    broken = "const a = 1;\nconst b = 2;\nconst c = 'open\n"
    assert unterminated_strings(broken) == [3]


def test_the_checker_is_not_fooled_by_prose_or_templates():
    fine = (
        "// don't worry, it's fine\n"
        "const a = `a template\nspanning lines`;\n"
        "const b = 'a normal string';\n"
        'const c = "it\'s escaped fine";\n'
        "const d = 'an escaped \\' quote';\n"
    )
    assert unterminated_strings(fine) == []


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_braces_and_brackets_balance(path: Path):
    """A crude but effective check that an edit did not truncate a file."""
    source = path.read_text(encoding="utf-8")
    # Strip strings, templates and comments before counting.
    stripped = re.sub(r"`(?:[^`\\]|\\.)*`", "``", source, flags=re.DOTALL)
    stripped = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", stripped)
    stripped = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', stripped)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    stripped = re.sub(r"//[^\n]*", "", stripped)

    for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
        assert stripped.count(opener) == stripped.count(closer), (
            f"{path.name} has unbalanced {opener}{closer}"
        )


# ---------------------------------------------------------------------------
# Promises the specification makes
# ---------------------------------------------------------------------------


def test_nothing_reaches_out_to_the_internet():
    """Spec section 14: no network request at runtime, no CDN, vendor it all.

    The only external-looking string allowed is the SVG namespace, which is an
    identifier rather than an address - nothing is fetched from it.
    """
    allowed = {"http://www.w3.org/2000/svg"}
    offenders: list[str] = []

    for path in [*JS_FILES, CSS, INDEX]:
        for match in re.finditer(r"https?://[^\s\"'<>)]+", path.read_text(encoding="utf-8")):
            url = match.group(0)
            if url in allowed:
                continue
            if url.startswith(("http://127.0.0.1", "http://localhost")):
                continue
            offenders.append(f"{path.name}: {url}")

    assert offenders == [], offenders


def test_the_page_loads_nothing_from_outside():
    html = INDEX.read_text(encoding="utf-8")
    for match in re.finditer(r'(?:src|href)="([^"]+)"', html):
        target = match.group(1)
        assert target.startswith(("/", "#", "data:")), (
            f"index.html loads {target}, which is not served from this computer"
        )


def test_the_readability_contract_is_in_the_stylesheet():
    """Spec section 10 calls these non-negotiable, so they are asserted."""
    css = CSS.read_text(encoding="utf-8")
    assert "--size-base: 17px" in css, "base font size"
    assert "--line: 1.6" in css, "line height"
    assert "--tap: 44px" in css, "minimum click target"
    assert "overflow-x: hidden" in css, "no horizontal scrolling"


def test_severity_is_never_colour_alone():
    """Spec section 10: no information conveyed by colour alone.

    Each severity tag carries a shape as well as a colour, through a ::before
    with content. The words come from the markup.
    """
    css = CSS.read_text(encoding="utf-8")
    for severity in ("critical", "high", "medium", "info"):
        assert f".tag--{severity}::before" in css, (
            f"{severity} has no shape, so it is distinguished by colour alone"
        )


def test_both_themes_define_every_colour():
    """A colour defined in one theme and not the other reads as black on black."""
    css = CSS.read_text(encoding="utf-8")

    light = css.split(":root {", 1)[1].split("}", 1)[0]
    dark = css.split(':root[data-theme="dark"] {', 1)[1].split("}", 1)[0]

    def names(block: str) -> set[str]:
        return {
            m.group(1) for m in re.finditer(r"(--[a-z0-9-]+):", block)
            if not m.group(1).startswith(("--font", "--size", "--sp-", "--line",
                                          "--tap", "--radius"))
        }

    missing = names(light) - names(dark)
    assert missing == set(), f"the dark theme does not define {sorted(missing)}"


def test_the_gap_hatching_exists_in_both_charts():
    """Spec 9.2: gap months are hatched and labelled, never smoothed over."""
    timeline = (WEB / "static/js/screens/timeline.js").read_text(encoding="utf-8")
    problems = (WEB / "static/js/screens/problems.js").read_text(encoding="utf-8")

    assert "gap-hatch" in timeline and "no data" in timeline
    assert "map-hatch" in problems and "no data" in problems


def test_the_timeline_draws_no_lines():
    """A bar chart cannot accidentally connect across a gap; a line chart can.

    The absence of path and polyline in the timeline is the structural reason
    the rendering rule holds, so it is asserted rather than trusted.
    """
    timeline = (WEB / "static/js/screens/timeline.js").read_text(encoding="utf-8")
    for element in ("'path'", '"path"', "'polyline'", "'polygon'"):
        assert element not in timeline, (
            f"the timeline creates {element}, which could draw across a gap"
        )


def test_every_screen_in_the_navigation_exists():
    """The navigation never offers a door that opens onto nothing."""
    main = (WEB / "static/js/main.js").read_text(encoding="utf-8")

    nav_block = main.split("const NAV = [", 1)[1].split("];", 1)[0]
    paths = re.findall(r"path:\s*'([^']+)'", nav_block)

    routes_block = main.split("const routes = {", 1)[1].split("};", 1)[0]
    routed = set(re.findall(r"'([^']*)':", routes_block))
    modules = set(re.findall(r"screens/([a-z]+)\.js", routes_block))

    for path in paths:
        assert path in routed, f"the navigation offers {path} with no route"

    for module in modules:
        assert (WEB / "static/js/screens" / f"{module}.js").exists(), (
            f"a route imports screens/{module}.js, which does not exist"
        )
