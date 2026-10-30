# The dreaming engine

This folder is part of agent-dreaming and is maintained here. It began as a copy of the dreaming scripts of
hermes-dreaming 3.8.0 by the same author (MIT, see `LICENSE`) and has no link to that project any more: nothing is
synced from it or installed from it, and changes are made in this repository.

Since 0.2.0 the engine takes its inputs directly (`dream.night()`): the person's messages, the memory notes, the fact
store's path, the answers to the previous night, a folder for its state. It reads no environment variable, no config
file and no agent home; the parts that served its first host (its config reader, message database, scheduled jobs,
cloud judge, per-chat stores, approval queue) were removed. `holo_screen.py` began as the screen of the holographic
memory provider (MIT, its authors) and is maintained here too.

`references/scoring.md` describes the scoring rules; its sections about the first host's files and jobs are history.
