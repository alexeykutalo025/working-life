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


# ---------------------------------------------------------------------------
# The two dark palettes
# ---------------------------------------------------------------------------


def theme_block(css: str, opener: str) -> dict:
    """The custom properties declared inside one block."""
    body = css.split(opener, 1)[1].split("}", 1)[0]
    return {
        m.group(1): m.group(2).strip()
        for m in re.finditer(r"(--[a-z0-9-]+):\s*([^;]+);", body)
    }


def test_the_two_dark_palettes_agree():
    """Dark is written twice: for the system setting, and for the toggle.

    Plain CSS has no way to name a block and reuse it, so the two can drift -
    and did. `--accent-hover` was `--accent` in one and `#a9caf3` in the other,
    which is not even a colour: it left every hover state broken for exactly
    the people who never touch the toggle, and nothing noticed.
    """
    css = CSS.read_text(encoding="utf-8")

    system = theme_block(css, ':root:not([data-theme="light"]) {')
    toggled = theme_block(css, ':root[data-theme="dark"] {')

    assert system, "the system-dark block has gone"
    assert toggled, "the explicit-dark block has gone"
    assert system == toggled, (
        "the two dark palettes disagree: "
        f"{ {k: (system.get(k), toggled.get(k)) for k in set(system) | set(toggled) if system.get(k) != toggled.get(k)} }"
    )


def test_every_colour_is_a_colour():
    """A var() that forgot its var() is not a colour, and fails silently."""
    css = CSS.read_text(encoding="utf-8")
    for opener in (":root {", ':root[data-theme="dark"] {',
                   ':root:not([data-theme="light"]) {'):
        for name, value in theme_block(css, opener).items():
            if name.startswith(("--font", "--size", "--sp-", "--line", "--tap",
                                "--radius", "--shadow")):
                continue
            assert not value.startswith("--"), (
                f"{name} in {opener} is {value!r} - it needs var({value})"
            )


# ---------------------------------------------------------------------------
# The pager's page numbers
# ---------------------------------------------------------------------------
#
# pageNumbers is the one piece of real logic in ui.js, and getting it wrong is
# not obvious from looking at the screen: an over-narrow window renders "1 2 …
# 9" on page one, which is tidy, plausible, and offers no way at all to reach
# page five. It is ported here rather than left untested.


def page_numbers(current: int, pages: int, span: int = 5) -> list:
    """The Python twin of pageNumbers() in ui.js."""
    if pages <= span + 2:
        return list(range(1, pages + 1))

    first = max(1, current - span // 2)
    last = first + span - 1
    if last > pages:
        last = pages
        first = max(1, last - span + 1)

    wanted = {1, pages} | set(range(first, last + 1))

    out: list = []
    previous = 0
    for page in sorted(wanted):
        if page - previous == 2:
            out.append(previous + 1)
        elif page - previous > 2:
            out.append(None)
        out.append(page)
        previous = page
    return out


def test_the_twin_matches_the_javascript():
    """If ui.js changes shape, this file is where it is noticed."""
    source = (WEB / "static/js/ui.js").read_text(encoding="utf-8")
    assert "export function pageNumbers(current, pages, span = 5)" in source, (
        "pageNumbers has changed signature; the Python twin below is now a lie"
    )


def test_a_short_list_shows_every_page():
    assert page_numbers(1, 1) == [1]
    assert page_numbers(1, 7) == [1, 2, 3, 4, 5, 6, 7]


def test_the_first_page_still_offers_a_run_of_pages():
    """"1 2 … 9" is the bug this guards: page five is then unreachable."""
    assert page_numbers(1, 9) == [1, 2, 3, 4, 5, None, 9]


def test_the_middle_is_surrounded_on_both_sides():
    assert page_numbers(5, 9) == [1, 2, 3, 4, 5, 6, 7, 8, 9]


def test_the_last_page_slides_the_run_back():
    assert page_numbers(9, 9) == [1, None, 5, 6, 7, 8, 9]


def test_a_very_long_list_stays_a_handful_of_buttons():
    for current in (1, 2, 150, 299, 300):
        shown = page_numbers(current, 300)
        assert len(shown) <= 9, f"page {current} of 300 rendered {len(shown)} slots"
        assert shown[0] == 1 and shown[-1] == 300, "both ends are always reachable"


def test_every_page_offered_is_a_real_page():
    for pages in (1, 5, 8, 9, 40, 300):
        for current in range(1, pages + 1):
            for page in page_numbers(current, pages):
                if page is None:
                    continue
                assert 1 <= page <= pages


def test_the_page_you_are_on_is_always_offered():
    for pages in (1, 5, 9, 40, 300):
        for current in range(1, pages + 1):
            assert current in page_numbers(current, pages), (
                f"page {current} of {pages} does not include itself"
            )


def test_a_gap_never_hides_a_single_page():
    """An ellipsis standing in for one page is worse than the page itself."""
    for pages in (9, 40, 300):
        for current in range(1, pages + 1):
            shown = page_numbers(current, pages)
            for i, page in enumerate(shown):
                if page is not None:
                    continue
                before, after = shown[i - 1], shown[i + 1]
                assert after - before > 2, (
                    f"page {current} of {pages}: … stands in for only "
                    f"{after - before - 1} page(s)"
                )
