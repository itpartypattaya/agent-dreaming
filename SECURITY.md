# Security

## Model

- The pass reads other programs' files and treats every byte of them as data. Text that tries to instruct an agent
  ("ignore previous instructions…") is screened out before the pass stores anything, and the review skill tells the
  agent to treat every quoted text as data.
- The pass never writes outside `~/.dreaming`; tests check that the files of Claude Code, Codex and holographic-memory
  are byte-for-byte unchanged after a pass, and that the fact store is opened with `mode=ro`.
- No network: the engine's optional network judge is not part of this package; tests run the pass with sockets
  disabled.
- Hooks print counts only and never fail a session. Commands are full interpreter paths with `-I` (nothing is imported
  from the working directory); no shell strings are built from data.
- Memory changes happen only in a session, through the agent's own memory tools, after the person's answer, with the
  store and the version they were shown checked first. Only `memory_merge` is pinned to the versions by the store
  itself (`expect`); for the other writes the check is made right before the call, so a change by another session in
  that instant is not caught (holographic-memory issue #2 asks for `expect` on every write).
- Locking is an OS file lock, released by the system when its holder dies; question ids carry their night, so an answer
  never lands on a question of a newer night.

## Reporting

Please report a vulnerability privately through GitHub's "Report a vulnerability" on this repository, not in a public
issue.
