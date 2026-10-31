"""agent-dreaming: a nightly, read-only memory review for Claude Code and Codex CLI.

The pass (``dreamem run``) reads what people said to the agents (Claude Code transcripts, Codex rollouts), the facts
holographic-memory stored and the CLIs' own memory notes, and runs the dreaming engine (``dreamem/engine``) on them in a
process, keeping its state under ``~/.dreaming``. It writes nothing anywhere else: changes to memory are made by the agent, in a
session, on a person's "yes" (``dreamem show`` / ``decide`` / ``applied``).
"""
__version__ = "0.6.0"
