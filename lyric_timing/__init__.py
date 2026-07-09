"""AI lyric-timing correction — optional subsystem, peer to spindlebot.

Pure logic (lrc parsing, audit heuristics, alignment post-processing) has no
heavy dependencies and is fully unit-tested. The real alignment backend
(Demucs + WhisperX) lives behind a Protocol in backends/ and is only imported
on demand; install its deps with setup-ai.sh.
"""
