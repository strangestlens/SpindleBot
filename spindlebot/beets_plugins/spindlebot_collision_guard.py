"""Beets adapter: use native destinations after metadata, before moving bytes.

Loaded only inside the external beet process, through a private temporary
configuration overlay. The core pipeline does not import beets.
"""

from __future__ import annotations

import os
from copy import copy
from pathlib import Path

from beets import ui
from beets.plugins import BeetsPlugin

from spindlebot.core.errors import SpindleBotError
from spindlebot.services.directory_collision import check_album_destinations


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
                commands.append(guarded)
        return commands

    def _check(self, sources, destinations):
        try:
            check_album_destinations(sources, destinations)
        except (SpindleBotError, OSError) as exc:
            raise ui.UserError(
                f"SpindleBot refused an unsafe album move: {exc}"
            ) from exc

    def check_import(self, session, task):
        items = list(task.imported_items())
        if items:
            self._check(
                [_path(item.path) for item in items],
                [_path(item.destination()) for item in items],
            )

    def check_move(self, item, source, destination):
        album = item.get_album()
        items = list(album.items()) if album else [item]
        self._check([_path(i.path) for i in items], [_path(destination)] * len(items))
