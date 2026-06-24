#!/bin/bash
# migrate-work-dirs.sh — sourceable migration helpers for the Phase A rename
# (Staging -> Import, Library -> Pending). Defines functions only; no top-level
# side effects, so it can be sourced by setup.sh and exercised by bats.

# move_contents FROM TO
# Relocates every entry (including dotfiles) out of FROM into TO without
# clobbering TO. Prints nothing and returns 1 (no-op) when there is nothing to
# move: TO empty/unset, FROM == TO, FROM missing, or FROM empty. Returns 0 only
# when it actually moved something.
move_contents() {
  local from="$1" to="$2"
  [ -z "$to" ] && return 1
  [ "$from" = "$to" ] && return 1
  [ ! -d "$from" ] && return 1
  [ -z "$(ls -A "$from" 2>/dev/null)" ] && return 1
  echo "Migrating contents: $from -> $to"
  mkdir -p "$to"
  ( shopt -s dotglob nullglob; mv "$from"/* "$to"/ 2>/dev/null ) \
    || rsync -a --remove-source-files "$from"/ "$to"/
  return 0
}

# migrate_work_dirs NEW_IMPORT NEW_PENDING BEETS_DB LEGACY_IMPORT LEGACY_PENDING
# Creates the new working dirs, relocates contents out of the legacy locations,
# and — only when the Pending area actually moved — reconciles beets DB item
# paths from the old Pending path to the new one. Idempotent and safe to re-run;
# never deletes the legacy top-level directories.
migrate_work_dirs() {
  local new_import="$1" new_pending="$2" beets_db="$3"
  local legacy_import="$4" legacy_pending="$5"

  [ -n "$new_import" ] && mkdir -p "$new_import"
  [ -n "$new_pending" ] && mkdir -p "$new_pending"

  move_contents "$legacy_import" "$new_import" || true

  if move_contents "$legacy_pending" "$new_pending"; then
    if [ -n "$beets_db" ] && [ -f "$beets_db" ] && command -v sqlite3 >/dev/null 2>&1; then
      if sqlite3 "$beets_db" \
          "UPDATE items SET path = replace(path, '${legacy_pending}', '${new_pending}') WHERE path LIKE '${legacy_pending}/%';"; then
        echo "Reconciled beets DB paths: $legacy_pending -> $new_pending"
      else
        echo "WARNING: beets DB path reconciliation failed — check manually." >&2
      fi
    fi
  fi
}
