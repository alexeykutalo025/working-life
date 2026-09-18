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
