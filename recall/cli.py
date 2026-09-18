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
