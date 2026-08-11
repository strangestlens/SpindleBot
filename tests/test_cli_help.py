"""A subcommand's --help must print help, never RUN the subcommand.

`spindlebot sync --help` used to execute a real sync: the flag was passed
straight through to a cmd_* that ignored it. Harmless on `inventory`, a
destructive surprise on `delete`.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from spindlebot.cli import main

# Every subcommand that takes flags and does real work.
SUBCOMMANDS = [
    "check", "import-staging", "finalize", "inventory", "review",
    "sync", "prune", "delete", "collection-audit", "collection-ignore",
    "fetch-lyrics", "fetch-art", "notify", "restart",
]


@pytest.mark.parametrize("command", SUBCOMMANDS)
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_subcommand_help_does_not_execute(command, flag, capsys):
    # config.load is patched to explode: help must not even get that far, so a
    # broken config still yields help rather than an error.
    with patch("spindlebot.config.load", side_effect=AssertionError("must not load config")):
        rc = main(["spindlebot", command, flag])

    assert rc == 0
    assert capsys.readouterr().out.strip(), "help must print something"


def test_top_level_help_still_works(capsys):
    for args in (["spindlebot"], ["spindlebot", "--help"], ["spindlebot", "help"]):
        assert main(args) == 0
        assert capsys.readouterr().out.strip()


def test_a_real_subcommand_invocation_is_unaffected():
    """The guard keys on -h/--help only; ordinary flags still dispatch."""
    with patch("spindlebot.config.load", return_value=object()), \
         patch("spindlebot.cli.cmd_inventory", return_value=0) as cmd:
        rc = main(["spindlebot", "inventory", "--location", "RetentionDrive", "--json"])

    assert rc == 0
    cmd.assert_called_once()
    assert cmd.call_args.args[1] == ["--location", "RetentionDrive", "--json"]
