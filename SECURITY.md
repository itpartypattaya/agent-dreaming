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
- Memory changes happen only in a session, through the agent's own tools, after the person's answer. In
  holographic-memory every write carries `expect` with the version the person was shown, and the store refuses a fact
  changed since, in the write's own transaction (0.5.0+). In a system without versions the check is the exact text:
  an MCP `memory` observation is deleted only when its text matches word for word (and the agent first checks that
  its session's server uses the file the night read); a Claude Code note is edited only after `dreamem verify` shows
  the file is the one the person was shown, by replacing its exact text — a delete empties the body that way first.
  What is left is the instant between that replacement and the delete of the emptied file; a file tool with a
  compare-and-swap delete would close it.
- Locking is an OS file lock, released by the system when its holder dies; question ids carry their night, so an answer
  never lands on a question of a newer night.

## Reporting

Please report a vulnerability privately through GitHub's "Report a vulnerability" on this repository, not in a public
issue.
