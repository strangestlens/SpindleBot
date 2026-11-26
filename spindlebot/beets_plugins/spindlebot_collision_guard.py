"""Beets adapter: use native destinations after metadata, before moving bytes.

Loaded only inside the external beet process, through a private temporary
configuration overlay. The core pipeline does not import beets.
"""

from __future__ import annotations

import os
from copy import copy
from pathlib import Path
from collections import defaultdict

from beets import ui
from beets.plugins import BeetsPlugin
from beets.util import normpath

from spindlebot.core.errors import SpindleBotError
from spindlebot.services.directory_collision import (
    IdentityCache,
    check_album_destinations,
)


def _path(value) -> Path:
    return Path(os.fsdecode(value))


class SpindlebotCollisionGuardPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        # Import stages run for ASIS as well as matched autotag imports. The
        # import_task_apply event alone does not fire for ASIS.
        self.early_import_stages = [self.check_import]
        self.import_stages = [self.check_import]
        self.register_listener("before_item_moved", self.check_move)
        self.register_listener("item_moved", self.record_move)
        self.register_listener("after_write", self.record_tag_write)
        self._identity = IdentityCache()
        self._participants = {}
        self._relocated = {}

    def commands(self):
        # A missing/broken plugin must not silently turn protection off. Beets
        # may log plugin-load failures and continue; these aliases only exist
        # when this adapter really loaded, so failed loading refuses the run.
        from beets.ui.commands import default_commands

        commands = []
        for command in default_commands:
            if command.name in {"import", "move"}:
                guarded = copy(command)
                guarded.name = f"spindlebot-guarded-{command.name}"
                guarded.aliases = []
                if command.name == "move":
                    native_move = command.func

                    def move(lib, opts, args):
                        if not opts.pretend:
                            self.preflight_move(lib, opts, args)
                        return native_move(lib, opts, args)

                    guarded.func = move
                commands.append(guarded)
        return commands

    def _check(self, sources, destinations, identities=None):
        try:
            return check_album_destinations(
                sources,
                destinations,
                identity=self._identity,
                identities=identities,
            )
        except (SpindleBotError, OSError) as exc:
            raise ui.UserError(
                f"SpindleBot refused an unsafe album move: {exc}"
            ) from exc

    def _check_items(self, items, destinations):
        sources = [_path(item.path) for item in items]
        identities = self._check(sources, destinations)
        for item, source in zip(items, sources):
            self._participants[item.id] = self._identity(source), identities

    def preflight_move(self, lib, opts, args):
        """Check the complete native query before any selected item is moved."""
        basedir = normpath(opts.dest) if opts.dest is not None else None
        if opts.album:
            items = [item for album in lib.albums(args) for item in album.items()]
        else:
            items = list(lib.items(args))
        groups = defaultdict(list)
        for item in items:
            groups[
                item.album_id if item.album_id is not None else ("item", item.id)
            ].append(item)
        self._participants.clear()
        for selected in groups.values():
            if not any(i.path != i.destination(basedir=basedir) for i in selected):
                continue
            # Selected items already in place can be the safe existing subset.
            # A missing, unmoved item contributes no audio to that directory.
            selected = [
                i
                for i in selected
                if _path(i.path).exists() or i.path != i.destination(basedir=basedir)
            ]
            self._check_items(
                selected, [_path(i.destination(basedir=basedir)) for i in selected]
            )

    def check_import(self, session, task):
        items = list(task.imported_items())
        if items:
            self._check_items(items, [_path(item.destination()) for item in items])

    def check_move(self, item, source, destination):
        source = _path(source)
        participant = self._participants.get(item.id)
        if participant is None:
            raise ui.UserError("SpindleBot refused a move outside the checked batch")
        expected, identities = participant
        try:
            if self._identity(source) != expected:
                raise ui.UserError(
                    "SpindleBot refused a source changed after preflight"
                )
        except OSError as exc:
            raise ui.UserError(
                f"SpindleBot could not verify move source: {exc}"
            ) from exc
        self._check([source], [_path(destination)], identities)

    def record_move(self, item, source, destination):
        if item.id in self._participants:
            self._relocated[item.id] = _path(destination)

    def record_tag_write(self, item, path=None):
        # Native import moves each track, then writes matched metadata to it.
        # With whole-file SHA fallback that legitimate write changes identity.
        # Accept it only at a participant's successfully moved destination;
        # changed sources before movement still fail the preflight snapshot.
        path = _path(path if path is not None else item.path)
        if self._relocated.get(item.id) != path:
            return
        _, identities = self._participants[item.id]
        try:
            written = self._identity(path)
        except OSError as exc:
            raise ui.UserError(
                f"SpindleBot could not verify tagged destination: {exc}"
            ) from exc
        identities.add(written)
        self._participants[item.id] = written, identities
