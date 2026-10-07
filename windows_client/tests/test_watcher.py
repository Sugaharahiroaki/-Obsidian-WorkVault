import unittest
from pathlib import Path

from watchdog.events import (
    DirCreatedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from watcher import RESCAN, Debouncer, VaultEventHandler


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class DebouncerTests(unittest.TestCase):
    def test_releases_after_quiet_period(self):
        clock = FakeClock()
        debouncer = Debouncer(2.5, clock=clock)
        debouncer.touch("a.md")
        clock.now = 2.0
        self.assertEqual(debouncer.pop_ready(), [])
        debouncer.touch("a.md")  # another save restarts the timer
        clock.now = 4.0
        self.assertEqual(debouncer.pop_ready(), [])
        clock.now = 4.5
        self.assertEqual(debouncer.pop_ready(), ["a.md"])
        self.assertEqual(debouncer.pop_ready(), [])

    def test_paths_are_independent(self):
        clock = FakeClock()
        debouncer = Debouncer(2.5, clock=clock)
        debouncer.touch("a.md")
        clock.now = 1.0
        debouncer.touch("b.md")
        clock.now = 2.6
        self.assertEqual(debouncer.pop_ready(), ["a.md"])
        clock.now = 3.6
        self.assertEqual(debouncer.pop_ready(), ["b.md"])


class EventHandlerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("C:/Vault") if Path("C:/").exists() else Path("/vault")
        self.clock = FakeClock()
        self.debouncer = Debouncer(0, clock=self.clock)
        self.handler = VaultEventHandler(self.root, self.debouncer, extra_excludes=("99_Archive/*",))

    def p(self, rel):
        return str(self.root / rel)

    def test_queues_relevant_note_events(self):
        self.handler.dispatch(FileModifiedEvent(self.p("01_Projects/A/project.md")))
        self.handler.dispatch(FileCreatedEvent(self.p("00_INBOX.md")))
        self.handler.dispatch(FileMovedEvent(self.p("old.md"), self.p("02_Ideas/new.md")))
        self.assertEqual(sorted(self.debouncer.pop_ready()), ["00_INBOX.md", "01_Projects/A/project.md", "02_Ideas/new.md"])

    def test_ignores_excluded_and_deleted(self):
        for event in [
            FileModifiedEvent(self.p(".obsidian/workspace.json")),
            FileModifiedEvent(self.p(".obsidian/x.md")),
            FileModifiedEvent(self.p("secrets.md")),
            FileModifiedEvent(self.p("img.png")),
            FileModifiedEvent(self.p("99_Archive/old.md")),
            FileDeletedEvent(self.p("a.md")),
            FileModifiedEvent(str(Path("D:/elsewhere/a.md"))),
        ]:
            self.handler.dispatch(event)
        self.assertEqual(self.debouncer.pop_ready(), [])

    def test_folder_changes_request_rescan(self):
        self.handler.dispatch(DirMovedEvent(self.p("Old"), self.p("New")))
        self.handler.dispatch(DirCreatedEvent(self.p("Fresh")))
        self.assertEqual(self.debouncer.pop_ready(), [RESCAN])


if __name__ == "__main__":
    unittest.main()
