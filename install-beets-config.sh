#!/bin/bash
# install-beets-config.sh — sourceable beets-config installer. Defines functions
# only; no top-level side effects, so it can be sourced by setup.sh and exercised
# by bats (same pattern as migrate-work-dirs.sh).

# install_beets_config TARGET TEMPLATE
# Copies TEMPLATE to TARGET when nothing is there yet, creating TARGET's parent.
# NEVER overwrites an existing beets config: it holds the user's Genius key and
# any local tuning, and clobbering it on a re-run of setup.sh would be silent
# data loss. setup.sh is documented as safe to re-run, so this must stay true.
#
# Returns 0 when it installed, 1 otherwise (nothing to do, or it could not).
# Prints a line either way so a setup.sh run says what happened.
install_beets_config() {
  local target="$1" template="$2"

  if [ -z "$target" ]; then
    echo "beets config path not configured — skipping (check tools.beets_config)."
    return 1
  fi
  if [ ! -f "$template" ]; then
    echo "beets config template missing: $template — skipping."
    return 1
  fi
  if [ -f "$target" ]; then
    echo "beets config already exists at $target — skipping."
    return 1
  fi

  mkdir -p "$(dirname "$target")" || return 1
  cp "$template" "$target" || return 1
  echo "Created $target — set lyrics.genius_api_key if you want beets' own lyrics fetch."
  return 0
}
