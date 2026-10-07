"""Filesystem watching (watchdog) with per-path debounce.

Obsidian saves every couple of seconds while you type, so a path is only
handed to the syncer after it has been quiet for ``debounce_seconds``.
"""
import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

import filters

logger = logging.getLogger("vault_sync.watcher")

# Debounce key meaning "rescan the whole vault" (NUL can never be in a path).
RESCAN = "\0rescan"


class Debouncer:
    """Releases each key once no new touch() arrived for ``delay`` seconds."""

    def __init__(self, delay, clock=time.monotonic):
        self._delay = delay
        self._clock = clock
        self._due = {}
        self._lock = threading.Lock()

    def touch(self, key):
        with self._lock:
            self._due[key] = self._clock() + self._delay

    def pop_ready(self):
        with self._lock:
            now = self._clock()
            ready = [key for key, due in self._due.items() if due <= now]
            for key in ready:
                del self._due[key]
        return ready

    def __len__(self):
        with self._lock:
            return len(self._due)


class VaultEventHandler(FileSystemEventHandler):
    """Feeds changed .md paths (vault-relative, "/" separated) into a Debouncer.

    Deletions are ignored: Phase 1 has no delete API.
    """

    def __init__(self, vault_root, debouncer, extra_excludes=()):
        super().__init__()
        self._root = Path(vault_root)
        self._debouncer = debouncer
        self._extra_excludes = extra_excludes

    def on_any_event(self, event):
        if event.event_type not in ("created", "modified", "moved"):
            return
        target = event.dest_path if event.event_type == "moved" else event.src_path
        if event.is_directory:
            # A folder appearing or being renamed carries notes that get no
            # events of their own; pick them up with a debounced rescan.
            if event.event_type in ("created", "moved"):
                self._debouncer.touch(RESCAN)
            return
        rel = self._relative(target)
        if rel and filters.path_exclusion_reason(rel, self._extra_excludes) is None:
            self._debouncer.touch(rel)

    def _relative(self, path):
        try:
            return Path(path).relative_to(self._root).as_posix()
        except ValueError:
            return None


def start_watching(vault_root, debouncer, extra_excludes=()):
    observer = Observer()
    observer.schedule(VaultEventHandler(vault_root, debouncer, extra_excludes), str(vault_root), recursive=True)
    observer.daemon = True
    observer.start()
    logger.info("Watching %s", vault_root)
    return observer
