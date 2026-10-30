# The dreaming engine

This folder is part of agent-dreaming and is maintained here. It began as a copy of the dreaming scripts of
hermes-dreaming 3.8.0 by the same author (MIT, see `LICENSE`), and has no link to that project any more: nothing is
synced from it, nothing here is installed from it, and changes are made in this repository.

`dream.py` still reads its input as the home of an agent it was first written for (`state.db`, `config.yaml`,
`dreaming.json`); `dreamem/shadow.py` builds that home from the Claude Code and Codex files. Replacing that layout with
direct inputs is planned work in this repository.
