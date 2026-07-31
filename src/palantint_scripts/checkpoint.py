"""
Shared resume/checkpoint helpers for scrapers that walk a list of items
(groups, calendars, ...) one at a time. Lets a scraper skip work it already
did on a prior run (unless a full re-sync is requested) and periodically
flushes progress to disk so a crash or interruption loses at most a handful
of items instead of the whole run.
"""

import json
import os


def load_json(path, default=None):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {} if default is None else default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


class ItemCheckpoint:
    """
    Hydrates a dict of previously-recorded items (keyed by id) from `path`,
    lets callers skip already-done items via `done()`, and writes progress
    back to disk every `save_every` calls to `record()` (plus whenever
    `flush()` is called explicitly, e.g. in a `finally` block).

    `to_disk` controls the on-disk shape: defaults to writing the raw
    id -> item dict, pass e.g. `lambda items: list(items.values())` for
    scrapers whose consumers expect a plain list. `from_disk` is the inverse,
    turning whatever `to_disk` last wrote back into the id -> item dict this
    class works with internally — required whenever `to_disk` isn't the
    identity function, e.g. `lambda data: {str(g["id"]): g for g in data}`
    for the list case above.
    """

    def __init__(self, path, save_every=5, to_disk=None, from_disk=None):
        self.path = path
        self.save_every = max(1, save_every)
        self._to_disk = to_disk or (lambda items: items)
        from_disk = from_disk or (lambda data: data)
        raw = load_json(path, default=None)
        self.items = from_disk(raw) if raw is not None else {}
        self._pending = 0

    def done(self, key):
        return key in self.items

    def get(self, key, default=None):
        return self.items.get(key, default)

    def record(self, key, value):
        self.items[key] = value
        self._pending += 1
        if self._pending >= self.save_every:
            self.flush()

    def flush(self):
        save_json(self.path, self._to_disk(self.items))
        self._pending = 0
