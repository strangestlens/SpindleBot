#!/usr/bin/env bats
# Tests for install-beets-config.sh — the sourceable helper setup.sh uses to put
# a beets config in place on a fresh machine.
#
# The property that matters is that it NEVER overwrites an existing beets config:
# that file holds the user's Genius key and any local tuning, setup.sh is
# documented as safe to re-run, and a clobber would be silent data loss.

LIB="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)/install-beets-config.sh"
TEMPLATE="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)/beets-config.yaml"

setup() {
  BATS_TMPDIR="$(mktemp -d)"
  # shellcheck source=/dev/null
  source "$LIB"
}

teardown() { rm -r "$BATS_TMPDIR"; }

@test "installs the template when nothing is there" {
  target="$BATS_TMPDIR/beets/config.yaml"
  run install_beets_config "$target" "$TEMPLATE"
  [ "$status" -eq 0 ]
  [ -f "$target" ]
  cmp -s "$target" "$TEMPLATE"
}

@test "creates the parent directory" {
  target="$BATS_TMPDIR/deeply/nested/config.yaml"
  install_beets_config "$target" "$TEMPLATE"
  [ -f "$target" ]
}

@test "NEVER overwrites an existing beets config" {
  target="$BATS_TMPDIR/config.yaml"
  echo "lyrics: {genius_api_key: my-real-key}" > "$target"
  run install_beets_config "$target" "$TEMPLATE"
  [ "$status" -eq 1 ]
  grep -qF "my-real-key" "$target"      # the user's file is untouched
}

@test "re-running is a no-op, not a clobber" {
  target="$BATS_TMPDIR/config.yaml"
  install_beets_config "$target" "$TEMPLATE"
  echo "# local tuning" >> "$target"
  # returns 1 for "nothing to do", same convention as move_contents in
  # migrate-work-dirs.sh — that is a no-op signal, not an error
  run install_beets_config "$target" "$TEMPLATE"
  [ "$status" -eq 1 ]
  grep -qF "# local tuning" "$target"
}

@test "no-ops when the config path is unset" {
  run install_beets_config "" "$TEMPLATE"
  [ "$status" -eq 1 ]
  [[ "$output" == *"not configured"* ]]
}

@test "no-ops when the template is missing" {
  run install_beets_config "$BATS_TMPDIR/config.yaml" "$BATS_TMPDIR/nope.yaml"
  [ "$status" -eq 1 ]
  [ ! -f "$BATS_TMPDIR/config.yaml" ]
}

@test "setup.sh calls the helper rather than inlining a cp" {
  setup_sh="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)/setup.sh"
  grep -qF "install_beets_config" "$setup_sh"
  # a bare cp of the template would bypass the never-overwrite guard
  ! grep -qE '^[[:space:]]*cp .*beets-config\.yaml' "$setup_sh"
}
