"""Talking to Outlook without hanging, and without leaving a mess.

The deadline behaviour is the headline, but the tests that matter most here are
the ones about ending a stranded Outlook. Closing the Outlook someone is
writing in would be the worst thing this program could do to the machine it
runs on, so the rule is narrow and tested from both sides:

    end it only if Recall started it AND nobody can see it.

Every test below either proves that an Outlook gets ended, or - more
importantly - proves that one does not.
"""

from __future__ import annotations

import time

import pytest

from recall import comguard
from recall.comguard import (
    ALREADY_RUNNING, CLEANED_UP, ComTimeout, Heartbeat, end_stranded_outlook,
    outlook_processes, run_with_timeout,
)


# --- the deadline ---------------------------------------------------------


def test_a_quick_call_just_returns():
    assert run_with_timeout(lambda: 21 * 2, timeout=5) == 42


def test_a_call_that_never_returns_raises_instead_of_hanging():
    started = time.monotonic()
    with pytest.raises(ComTimeout):
        run_with_timeout(lambda: time.sleep(30), timeout=0.5)
    assert time.monotonic() - started < 10, "the deadline did not hold"


def test_the_timeout_message_says_what_to_do():
    with pytest.raises(ComTimeout) as caught:
        run_with_timeout(lambda: time.sleep(30), timeout=0.5)
    assert "What to do" in str(caught.value)


def test_an_error_inside_comes_back_as_itself():
    """A real failure must not be disguised as a timeout."""
    def boom():
        raise KeyError("no such folder")

    with pytest.raises(KeyError):
        run_with_timeout(boom, timeout=5)


def test_a_slow_call_that_keeps_moving_is_not_cut_off():
    """A 40 GB mailbox legitimately takes hours; only a wedged call is killed."""
    def slow(pulse: Heartbeat):
        for _ in range(6):
            time.sleep(0.1)
            pulse.beat()
        return "finished"

    assert run_with_timeout(slow, timeout=0.5, heartbeat=True) == "finished"


def test_a_call_that_stops_moving_is_cut_off():
    def stalls(pulse: Heartbeat):
        pulse.beat()
        time.sleep(30)

    with pytest.raises(ComTimeout):
        run_with_timeout(stalls, timeout=0.5, heartbeat=True)


# --- ending a stranded Outlook --------------------------------------------


@pytest.fixture
def fake_outlook(monkeypatch):
    """Stand-in for the machine's Outlook processes and their windows."""

    class World:
        def __init__(self):
            self.running: set[int] = set()
            self.visible: set[int] = set()
            self.ended: list[int] = []

        def install(self):
            monkeypatch.setattr(comguard, "outlook_processes", lambda: set(self.running))
            monkeypatch.setattr(
                comguard, "has_visible_window", lambda pid: pid in self.visible
            )

            def kill(args, **kwargs):
                pid = int(args[2])
                self.ended.append(pid)
                self.running.discard(pid)
                return None

            monkeypatch.setattr(comguard.subprocess, "run", kill)

    world = World()
    world.install()
    return world


def test_an_outlook_we_started_with_no_window_is_ended(fake_outlook):
    fake_outlook.running = {4242}
    assert end_stranded_outlook(started_before=set()) == [4242]
    assert fake_outlook.ended == [4242]


def test_an_outlook_that_was_already_running_is_left_alone(fake_outlook):
    """It may be the user's own, mid-sentence. Recall never touches it."""
    fake_outlook.running = {4242}
    assert end_stranded_outlook(started_before={4242}) == []
    assert fake_outlook.ended == []


def test_an_outlook_with_a_window_is_left_alone(fake_outlook):
    """Someone is looking at it, so it is not stranded however it got there."""
    fake_outlook.running = {4242}
    fake_outlook.visible = {4242}
    assert end_stranded_outlook(started_before=set()) == []
    assert fake_outlook.ended == []


def test_only_the_stranded_one_is_ended(fake_outlook):
    """The user's Outlook and ours, side by side. Exactly one is ended."""
    theirs, visible_new, ours = 100, 200, 300
    fake_outlook.running = {theirs, visible_new, ours}
    fake_outlook.visible = {theirs, visible_new}

    assert end_stranded_outlook(started_before={theirs}) == [ours]
    assert fake_outlook.ended == [ours]
    assert theirs in fake_outlook.running and visible_new in fake_outlook.running


def test_nothing_running_ends_nothing(fake_outlook):
    assert end_stranded_outlook(started_before=set()) == []
    assert fake_outlook.ended == []


def test_a_window_that_cannot_be_checked_counts_as_visible(monkeypatch):
    """Undecidable must never mean "end it"."""
    monkeypatch.setitem(__import__("sys").modules, "win32gui", None)
    monkeypatch.setitem(__import__("sys").modules, "win32process", None)
    assert comguard.has_visible_window(4242) is True


def test_listing_failure_makes_everything_untouchable(monkeypatch):
    """If tasklist cannot be run, no process can be shown to be ours."""
    def explode(*args, **kwargs):
        raise OSError("tasklist is missing")

    monkeypatch.setattr(comguard.subprocess, "run", explode)
    assert outlook_processes() == set()
    assert end_stranded_outlook(started_before=set()) == []


# --- what the user is told ------------------------------------------------


def test_cleaning_up_is_explained(fake_outlook):
    fake_outlook.running = {4242}
    assert comguard._cleanup_note(set()) == CLEANED_UP


def test_leaving_one_alone_is_explained(fake_outlook):
    """The advice has to change: Recall cannot clear this one for them."""
    fake_outlook.running = {4242}
    fake_outlook.visible = {4242}
    note = comguard._cleanup_note({4242})
    assert note == ALREADY_RUNNING
    assert "Task Manager" in note


def test_no_outlook_at_all_adds_no_excuse(fake_outlook):
    assert comguard._cleanup_note(set()) == ""


# --- asking Outlook to quit -----------------------------------------------
#
# outlook_version starts Outlook just to read a version string, so it puts it
# back. The guard on that Quit is the most dangerous line in this module: quit
# the wrong instance and the user loses the message they were writing.


class FakeOutlookApp:
    def __init__(self):
        self.quit_called = False

    @property
    def Version(self):
        return "16.0.0.20326"

    def Quit(self):
        self.quit_called = True


@pytest.fixture
def fake_com(monkeypatch, fake_outlook):
    """A stand-in for win32com.client.

    Dispatching starts a process, exactly as the real one does - that is the
    whole reason this cleanup exists, so the fake has to do it too.
    """
    import sys
    import types

    app = FakeOutlookApp()
    app.spawns = 7001

    def dispatch(progid):
        # COM attaches to a running Outlook and only starts one when there is
        # none - which is exactly why "did a new process appear" identifies
        # the instance Recall is responsible for.
        if not fake_outlook.running:
            fake_outlook.running.add(app.spawns)
        return app

    module = types.ModuleType("win32com.client")
    module.Dispatch = dispatch
    parent = types.ModuleType("win32com")
    parent.client = module
    monkeypatch.setitem(sys.modules, "win32com", parent)
    monkeypatch.setitem(sys.modules, "win32com.client", module)
    return app


def test_an_outlook_we_started_is_asked_to_quit(fake_com, fake_outlook):
    """A health check must not leave another invisible Outlook resident."""
    assert comguard.outlook_version(timeout=5) == "16.0.0.20326"
    assert fake_com.quit_called, "Recall left its own Outlook running"


def test_the_users_own_outlook_is_never_asked_to_quit(fake_com, fake_outlook):
    """They may be halfway through writing something. Do not touch it."""
    fake_outlook.running = {4242}
    fake_outlook.visible = {4242}

    assert comguard.outlook_version(timeout=5) == "16.0.0.20326"
    assert not fake_com.quit_called, "Recall quit the user's own Outlook"
    assert fake_outlook.ended == []
    assert 4242 in fake_outlook.running


def test_an_outlook_that_refuses_to_quit_is_swept_up(fake_com, fake_outlook, monkeypatch):
    """Quit is a request, not a command; the sweep is what guarantees it."""
    def refuse():
        raise RuntimeError("Outlook is busy")

    monkeypatch.setattr(fake_com, "Quit", refuse)

    assert comguard.outlook_version(timeout=5) == "16.0.0.20326"
    assert fake_outlook.ended == [fake_com.spawns]


def test_a_running_outlook_survives_a_check_that_goes_wrong(
    fake_com, fake_outlook, monkeypatch
):
    """Their Outlook is running and invisible - a minimised one, say.

    Recall attaches to it rather than starting its own, so there is nothing of
    Recall's to clean up, and the sweep must end nothing even though the only
    Outlook on the machine is windowless and Quit failed.
    """
    def refuse():
        raise RuntimeError("Outlook is busy")

    monkeypatch.setattr(fake_com, "Quit", refuse)
    fake_outlook.running = {4242}          # theirs, already there, no window

    assert comguard.outlook_version(timeout=5) == "16.0.0.20326"
    assert fake_outlook.ended == []
    assert 4242 in fake_outlook.running, "Recall ended the user's Outlook"
