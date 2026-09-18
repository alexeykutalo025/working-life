"""The `recall` command line.

Every command here does the whole job or explains exactly why it cannot. None of
them prints a number it cannot substantiate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from . import __version__
from .config import ConfigError, Settings, load_settings
from .logging_setup import setup_logging

app = typer.Typer(
    name="recall",
    help="Recall - find, read and search a lifetime of Outlook files. "
    "Everything happens on this computer; nothing is sent anywhere.",
    no_args_is_help=True,
    add_completion=False,
)

_state: dict[str, object] = {}


def _settings() -> Settings:
    s = _state.get("settings")
    if s is None:
        raise RuntimeError("settings were not loaded; this is a bug in cli.py")
    return s  # type: ignore[return-value]


@app.callback()
def main(
    ctx: typer.Context,
    config: Path = typer.Option(
        None,
        "--config",
        "-c",
        help="Use a different settings file instead of config.toml.",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Print less to the screen. The log file is unchanged."
    ),
) -> None:
    """Load settings and start logging before any command runs."""
    try:
        settings = load_settings(config)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    settings.ensure_workdir()
    setup_logging(
        settings.logs_path,
        file_level=settings.logging.file_level,
        console_level=settings.logging.console_level,
        keep_days=settings.logging.keep_days,
        quiet=quiet,
    )
    _state["settings"] = settings
    _state["quiet"] = quiet


@app.command()
def version() -> None:
    """Print the version of Recall."""
    typer.echo(f"Recall {__version__}")


@app.command()
def doctor() -> None:
    """Check this computer has everything Recall needs. Prints a pass/fail table."""
    from .doctor import doctor_report

    table, ok = doctor_report(_settings())
    typer.echo(table)
    raise typer.Exit(code=0 if ok else 1)


@app.command()
def scan(
    roots: list[str] = typer.Option(
        None,
        "--roots",
        "-r",
        help="Folders or drives to look in, for example:  --roots D: --roots C:\\Users . "
        "Leave this out to search everywhere listed in config.toml.",
    ),
    full_hash: bool = typer.Option(
        False,
        "--full-hash",
        help="Fingerprint even very large files, so duplicates among them can be "
        "found. Slower.",
    ),
) -> None:
    """Find every Outlook file on this computer. Reads names and sizes only."""
    from .db import connect
    from .scan.walker import Scanner

    settings = _settings()
    root_paths = [Path(r) for r in roots] if roots else settings.effective_scan_roots()
    missing = [p for p in root_paths if not p.exists()]
    root_paths = [p for p in root_paths if p.exists()]

    if missing:
        typer.echo("These places do not exist and will be skipped:")
        for m in missing:
            typer.echo(f"  {m}")
    if not root_paths:
        typer.secho("There is nowhere to look.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    typer.echo("Looking in:")
    for p in root_paths:
        typer.echo(f"  {p}")
    typer.echo("")
    typer.echo("This can take a long time on a full drive. Press Ctrl-C to stop;")
    typer.echo("everything found so far is kept, and running it again carries on.")
    typer.echo("")

    conn = connect(settings.db_path)
    scanner = Scanner(settings, conn)
    try:
        result = scanner.run(root_paths, full_hash=full_hash)
    except KeyboardInterrupt:
        scanner.cancel.set()
        result = scanner.progress
        typer.echo("")
        typer.echo("Stopping cleanly...")
    finally:
        conn.close()

    typer.echo("")
    typer.echo(result.message or "Finished.")
    if result.unreadable_dirs:
        typer.echo(
            f"{result.unreadable_dirs:,} folder(s) could not be opened and were "
            "skipped. Every one is listed in the log."
        )
    typer.echo(f"Looked at {result.files_seen:,} files in {result.dirs_seen:,} folders.")
    raise typer.Exit(code=0 if result.state in ("done", "canceled") else 1)


@app.command()
def extract(
    kinds: str = typer.Option(
        None,
        "--kinds",
        help="What to read: calendar, contacts, mail, tasks, notes. "
        "Separate several with commas. The default is everything.",
    ),
    sources: str = typer.Option(
        None,
        "--sources",
        help="Only these files, by the id shown on the Files found screen. "
        "Separate several with commas.",
    ),
    sample: int = typer.Option(
        0,
        "--sample",
        help="Read only the first N records from each file. A quick way to check "
        "everything works before starting a long run.",
    ),
    resume: bool = typer.Option(
        True,
        "--resume/--restart",
        help="--resume skips files already read. --restart reads every chosen "
        "file again; nothing is duplicated either way.",
    ),
) -> None:
    """Read your Outlook files into the archive. Stop any time with Ctrl-C."""
    import threading

    from .db import connect
    from .extract import Extractor, parse_kinds

    settings = _settings()

    try:
        wanted = parse_kinds(kinds)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    source_ids = None
    if sources:
        try:
            source_ids = [int(s.strip()) for s in sources.split(",") if s.strip()]
        except ValueError as exc:
            typer.secho(
                "--sources takes the numbers shown on the Files found screen, "
                f"for example --sources 3,7,12. Got: {sources!r}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2) from exc

    cancel = threading.Event()
    last_line = {"text": ""}

    def show(progress) -> None:
        line = (
            f"  {progress.files_done}/{progress.files_total} files  "
            f"{progress.items_written:,} records  "
            f"{progress.items_per_sec:,.0f}/sec  "
            f"{Path(progress.current_file).name}"
        )
        if line != last_line["text"]:
            typer.echo(line[:140].ljust(len(last_line["text"])), nl=False)
            typer.echo("\r", nl=False)
            last_line["text"] = line

    conn = connect(settings.db_path)
    extractor = Extractor(settings, conn, cancel=cancel, on_progress=show)

    typer.echo(f"Reading: {', '.join(sorted(wanted))}")
    if sample:
        typer.echo(f"Sampling the first {sample} records from each file.")
    typer.echo("Press Ctrl-C at any time. Everything read so far is kept.")
    typer.echo("")

    try:
        result = extractor.run(
            kinds=wanted, source_ids=source_ids, sample=sample, resume=resume
        )
    except KeyboardInterrupt:
        cancel.set()
        result = extractor.progress
        typer.echo("")
        typer.echo("Stopping cleanly...")
    finally:
        conn.close()

    typer.echo("")
    typer.echo(result.message or "Finished.")
    for path, reason in result.failures:
        typer.secho(f"  could not read {path}: {reason}", fg=typer.colors.YELLOW)
    raise typer.Exit(code=0 if result.state in ("done", "canceled") else 1)


@app.command()
def audit(
    checks: str = typer.Option(
        None,
        "--checks",
        help="Which checks to run: corrupt, gaps, accounts, quality. "
        "Separate several with commas. The default is all of them.",
    ),
    report: Path = typer.Option(
        None, "--report", help="Also write the report to this file (.md or .txt)."
    ),
) -> None:
    """Check the archive for problems and print what is wrong.

    Exits with a non-zero code if anything critical is still open, so this can
    be used to decide whether the archive is fit to rely on.
    """
    from .db import connect
    from .integrity.report import format_markdown, format_report, run_audit

    settings = _settings()
    wanted = None
    if checks:
        wanted = {c.strip().lower() for c in checks.split(",") if c.strip()}

    conn = connect(settings.db_path)
    try:
        try:
            result = run_audit(conn, settings, checks=wanted)
        except ValueError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from exc
    finally:
        conn.close()

    text = format_report(result)
    typer.echo(text)

    if report:
        report = Path(report)
        report.parent.mkdir(parents=True, exist_ok=True)
        if report.suffix.lower() in (".md", ".markdown"):
            report.write_text(format_markdown(result), encoding="utf-8")
        else:
            report.write_text(text, encoding="utf-8")
        typer.echo("")
        typer.echo(f"Written to {report}")

    raise typer.Exit(code=1 if result["has_critical"] else 0)


findings_app = typer.Typer(
    help="Look at and answer the problems Recall has found.", no_args_is_help=True
)
app.add_typer(findings_app, name="findings")


@findings_app.command("list")
def findings_list(
    severity: str = typer.Option(None, "--severity", help="critical, high, medium or info."),
    state: str = typer.Option("open", "--state", help="open, explained, resolved, wont_fix or all."),
    code: str = typer.Option(None, "--code", help="Only this kind of problem."),
    limit: int = typer.Option(100, "--limit"),
) -> None:
    """List the problems found, worst first."""
    from .db import connect

    settings = _settings()
    conn = connect(settings.db_path)
    try:
        where = []
        params: list = []
        if state == "open":
            where.append("state IN ('open','acknowledged')")
        elif state != "all":
            where.append("state = ?")
            params.append(state)
        if severity:
            where.append("severity = ?")
            params.append(severity)
        if code:
            where.append("code = ?")
            params.append(code)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        rows = conn.execute(
            f"SELECT id, code, severity, title, estimated_loss, affected_count, state "
            f"FROM findings {where_sql} "
            f"ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            f"WHEN 'medium' THEN 2 ELSE 3 END, id LIMIT ?",
            (*params, limit),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        typer.echo("No problems match that.")
        return

    for row in rows:
        marker = {
            "critical": typer.colors.RED,
            "high": typer.colors.YELLOW,
        }.get(row["severity"])
        typer.secho(
            f"  [{row['id']:>4}] {row['severity']:<8} {row['title']}", fg=marker
        )
        extra = []
        if row["estimated_loss"]:
            extra.append(f"about {row['estimated_loss']:,} records unreadable")
        if row["affected_count"]:
            extra.append(f"{row['affected_count']:,} affected")
        if row["state"] != "open":
            extra.append(row["state"])
        if extra:
            typer.echo(f"         {'; '.join(extra)}")

    typer.echo("")
    typer.echo(f"{len(rows)} problem(s). To say what caused one:")
    typer.echo(f'    recall findings explain {rows[0]["id"]} "what was happening"')


@findings_app.command("explain")
def findings_explain(
    finding_id: int = typer.Argument(..., help="The problem's number."),
    note: str = typer.Argument(..., help="What was happening, in your own words."),
) -> None:
    """Record what caused a problem. It stays visible; it stops nagging."""
    from .db import connect
    from .integrity.engine import set_finding_state

    settings = _settings()
    conn = connect(settings.db_path)
    try:
        try:
            changed = set_finding_state(conn, finding_id, "explained", note)
        except ValueError as exc:
            typer.secho(str(exc), fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from exc
    finally:
        conn.close()

    if not changed:
        typer.secho(f"There is no problem numbered {finding_id}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.echo("Noted. It will stop appearing in the health banner.")
    typer.echo("It stays on the timeline, with your note, permanently.")


@findings_app.command("acknowledge")
def findings_acknowledge(
    finding_id: int = typer.Argument(...),
    note: str = typer.Option(None, "--note"),
) -> None:
    """Mark a problem as seen. It stays open and still qualifies the counts."""
    _set_state(finding_id, "acknowledged", note)


@findings_app.command("wont-fix")
def findings_wont_fix(
    finding_id: int = typer.Argument(...),
    note: str = typer.Option(None, "--note"),
) -> None:
    """Mark a problem as one you are not going to do anything about."""
    _set_state(finding_id, "wont_fix", note)


def _set_state(finding_id: int, state: str, note: str | None) -> None:
    from .db import connect
    from .integrity.engine import set_finding_state

    settings = _settings()
    conn = connect(settings.db_path)
    try:
        changed = set_finding_state(conn, finding_id, state, note)
    finally:
        conn.close()
    if not changed:
        typer.secho(f"There is no problem numbered {finding_id}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Problem {finding_id} is now marked {state}. The record is kept.")


@findings_app.command("retry")
def findings_retry(
    finding_id: int = typer.Argument(..., help="The problem's number."),
) -> None:
    """Read the file again with the other reader, and see if it does better."""
    from .db import connect
    from .extract import Extractor

    settings = _settings()
    conn = connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT f.source_file_id, sf.path, sf.parse_backend, sf.item_count "
            "FROM findings f JOIN source_files sf ON sf.id = f.source_file_id "
            "WHERE f.id = ?",
            (finding_id,),
        ).fetchone()
        if row is None:
            typer.secho(
                f"Problem {finding_id} is not about a file, so there is nothing "
                "to read again.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=2)

        was_backend = row["parse_backend"]
        was_count = int(row["item_count"] or 0)
        other = "com" if was_backend == "pypff" else "pypff"

        typer.echo(f"Reading {row['path']} again.")
        typer.echo(f"It was read with: {was_backend or 'unknown'}, giving {was_count:,} records.")
        typer.echo(f"Trying: {other}")
        typer.echo("")

        settings.extract.pst_backend = other
        conn.execute(
            "UPDATE source_files SET parse_state = 'pending' WHERE id = ?",
            (row["source_file_id"],),
        )
        result = Extractor(settings, conn).run(source_ids=[int(row["source_file_id"])])

        after = conn.execute(
            "SELECT item_count, parse_backend FROM source_files WHERE id = ?",
            (row["source_file_id"],),
        ).fetchone()
        now_count = int(after["item_count"] or 0)
    finally:
        conn.close()

    typer.echo("")
    typer.echo(result.message)
    if now_count > was_count:
        typer.secho(
            f"Better: {now_count:,} records now, against {was_count:,} before. "
            f"The extra {now_count - was_count:,} have been added.",
            fg=typer.colors.GREEN,
        )
        typer.echo("The problem stays on the list, with a note that a retry did better.")
    elif now_count == was_count:
        typer.echo(
            f"The same: {now_count:,} records, as before. The other reader did no "
            "better, so what is missing is genuinely unreadable."
        )
    else:
        typer.secho(
            f"Worse: {now_count:,} records, against {was_count:,} before. The "
            "original result was better and has been kept.",
            fg=typer.colors.YELLOW,
        )


people_app = typer.Typer(
    help="Look at and correct who is who in the archive.", no_args_is_help=True
)
app.add_typer(people_app, name="people")


@people_app.command("suggest")
def people_suggest(
    limit: int = typer.Option(50, "--limit", help="How many suggestions to show."),
) -> None:
    """Show people who may be listed twice. Merges nothing."""
    from .db import connect
    from .normalize.merge import suggest_merges

    settings = _settings()
    conn = connect(settings.db_path)
    try:
        proposals = suggest_merges(conn, settings, limit=limit)
        if not proposals:
            typer.echo("Nobody looks like they are listed twice.")
            return

        typer.echo(
            f"{len(proposals)} suggestion(s). Nothing has been merged - these are "
            "guesses, with the evidence shown."
        )
        typer.echo("")
        for p in proposals:
            names = {
                int(r["id"]): (r["display_name"] or "(no name)")
                for r in conn.execute(
                    "SELECT id, display_name FROM people WHERE id IN (?, ?)",
                    (p.person_a, p.person_b),
                )
            }
            typer.echo(
                f"  [{p.confidence:.2f}]  {names.get(p.person_a)} (#{p.person_a})"
                f"  <->  {names.get(p.person_b)} (#{p.person_b})"
            )
            typer.echo(f"           {p.reason}")
            caution = p.evidence.get("caution")
            if caution:
                typer.secho(f"           {caution}", fg=typer.colors.YELLOW)
            typer.echo(
                f"           to join them:  recall people merge "
                f"--keep {p.person_a} --merge {p.person_b}"
            )
            typer.echo("")
    finally:
        conn.close()


@people_app.command("merge")
def people_merge(
    keep: int = typer.Option(..., "--keep", help="The person to keep."),
    merge: int = typer.Option(..., "--merge", help="The person to join into them."),
    note: str = typer.Option(None, "--note", help="Why, for the record."),
) -> None:
    """Join two people into one. Undo with:  recall people unmerge <id>"""
    from .db import connect
    from .normalize.merge import MergeError, apply_merge

    settings = _settings()
    conn = connect(settings.db_path)
    try:
        result = apply_merge(conn, keep, merge, note=note)
    except MergeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    finally:
        conn.close()

    typer.echo(
        f"Joined {result['merged_name']} (#{result['merged']}) into "
        f"{result['kept_name']} (#{result['kept']})."
    )
    typer.echo(f"Nothing was deleted. To undo:  recall people unmerge {result['merged']}")


@people_app.command("unmerge")
def people_unmerge(
    person_id: int = typer.Argument(..., help="The person to separate out again."),
) -> None:
    """Separate a person who was merged into someone else."""
    from .db import connect
    from .normalize.merge import MergeError, undo_merge

    settings = _settings()
    conn = connect(settings.db_path)
    try:
        result = undo_merge(conn, person_id)
    except MergeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    finally:
        conn.close()
    typer.echo(f"Person #{result['separated']} is separate again.")


@app.command(name="export")
def export_cmd(
    format: str = typer.Argument(
        ..., help="csv, xlsx, markdown or json."
    ),
    kind: str = typer.Option(
        "calendar", "--kind", help="calendar, mail or contacts."
    ),
    out: Path = typer.Option(
        None, "--out", help="Where to write it. The default is workdir/exports."
    ),
    basic: bool = typer.Option(
        False,
        "--basic",
        help="Write only the columns the specification fixes, leaving out the "
        "extra detail columns.",
    ),
) -> None:
    """Save part of the archive to a file, with its integrity statement."""
    from .db import connect
    from .export import ExportError, run_export

    settings = _settings()
    conn = connect(settings.db_path)
    try:
        data_path, statement_path, statement = run_export(
            conn, settings, fmt=format, kind=kind, out_path=out, full=not basic
        )
    except ExportError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    except OSError as exc:
        typer.secho(f"The file could not be written: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    finally:
        conn.close()

    typer.echo(f"Wrote {statement.exported_count:,} records to:")
    typer.echo(f"  {data_path}")
    if statement_path:
        typer.echo("What is missing or uncertain in it is written beside it:")
        typer.echo(f"  {statement_path}")

    if not statement.is_clean:
        typer.echo("")
        if statement.estimated_missing:
            typer.secho(
                f"This export is NOT complete. About "
                f"{statement.estimated_missing:,} more records could not be read.",
                fg=typer.colors.YELLOW,
            )
        for q in statement.qualifiers:
            typer.secho(f"  - {q.text}", fg=typer.colors.YELLOW)
        if statement.undated_count:
            typer.secho(
                f"  - {statement.undated_count:,} record(s) have no date and are "
                "in no date range.",
                fg=typer.colors.YELLOW,
            )


@app.command()
def serve(
    port: int = typer.Option(None, "--port", "-p", help="Which port to listen on."),
    open_browser: bool = typer.Option(
        False, "--open", help="Open the page in your browser once it is ready."
    ),
) -> None:
    """Start the local web page. Reachable from this computer only."""
    from .api.app import serve as run_server

    settings = _settings()
    port = port or settings.server.port
    typer.echo(f"Recall is at  http://{settings.server.host}:{port}")
    typer.echo("Leave this window open. Press Ctrl-C to stop.")
    try:
        run_server(settings, port=port, open_browser=open_browser or settings.server.open_browser)
    except OSError as exc:
        if getattr(exc, "errno", None) in (48, 98, 10048):
            typer.secho(
                f"Port {port} is already being used by something else.\n"
                "Either close that program, or start Recall on a different port:\n"
                f"    recall serve --port {port + 1}",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1) from exc
        raise
    except KeyboardInterrupt:
        typer.echo("\nRecall stopped.")


@app.command()
def reset(
    items: bool = typer.Option(
        False,
        "--items",
        help="Delete extracted mail, calendar, contacts, people and findings. "
        "The list of files found by scanning is kept.",
    ),
    all_: bool = typer.Option(
        False,
        "--all",
        help="Delete the whole archive, including the list of files found. "
        "Your original Outlook files are never touched.",
    ),
) -> None:
    """Empty the archive and start again. Asks you to type a word to confirm."""
    from .reset import reset_all, reset_items

    settings = _settings()
    if items == all_:
        typer.secho(
            "Choose one: --items (keep the file list) or --all (start completely over).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    if all_:
        what = "the entire archive, including the list of files found"
        word = "DELETE EVERYTHING"
    else:
        what = "all extracted mail, calendar entries, contacts, people and findings"
        word = "DELETE ITEMS"

    typer.echo(f"This will permanently delete {what}.")
    typer.echo("Your original Outlook files will NOT be touched.")
    typer.echo("")
    typed = typer.prompt(f"Type  {word}  to continue, or anything else to stop")
    if typed.strip() != word:
        typer.echo("Nothing was deleted.")
        raise typer.Exit(code=1)

    if all_:
        summary = reset_all(settings)
    else:
        summary = reset_items(settings)
    typer.echo(summary)


def run() -> None:
    """Entry point used by start.bat and `python -m recall`."""
    try:
        app()
    except KeyboardInterrupt:
        # Spec section 12: Ctrl-C finishes the current transaction and exits
        # cleanly. Each long-running command handles its own cancellation; this
        # is the last resort so the user never sees a traceback.
        typer.echo("\nStopped. Nothing was lost - run the same command again to continue.")
        sys.exit(130)


if __name__ == "__main__":
    run()
