---
name: force-import
description: Force-imports the most recent XLD rip from ~/Music/Staging into the music library by running `python -m spindlebot import --force`. Use this skill whenever the user types /force-import, "force import", "spindlebot force", or asks to import a rip that's stuck waiting on a multi-disc check. Also use it when the user provides a specific .log file path they want to force-import.
---

# SpindleBot Force Import

Skip the multi-disc disc-check and immediately import an XLD rip into the music library.

## What this does

Runs `python -m spindlebot import <log-path> --force` from the music-pipeline directory. The `--force` flag bypasses the disc-presence check, which is useful when you want to import a single-disc rip immediately, or when you know all discs are present but the watcher hasn't fired.

## Steps

1. **Determine the log file path**
   - If the user provided a path as an argument, use that.
   - Otherwise, find the most recent `.log` file in `~/Music/Staging`:
     ```bash
     ls -t ~/Music/Staging/*.log 2>/dev/null | head -1
     ```
   - If no `.log` files are found, tell the user: "No .log files found in ~/Music/Staging. Make sure XLD has finished ripping, or provide a path directly."

2. **Confirm with the user** (one line is fine)
   - Show the filename (not full path) you're about to import, e.g.: `Importing: Mezzanine - Mezzanine XX／2018 remaster.log`

3. **Run the import**
   ```bash
   cd /Users/danielwilliams/Music/music-pipeline && python -m spindlebot import "<log-path>" --force
   ```
   Stream or show the full output so the user can see what beets/the pipeline does.

4. **Report the result**
   - If it succeeded, note what was imported.
   - If it failed, show the relevant error output.
