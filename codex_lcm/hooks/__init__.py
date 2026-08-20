"""Codex CLI hook handlers for codex-lcm.

Each hook is invoked by Codex as `python -m codex_lcm.hooks.<event_name>` and
reads its JSON payload from stdin. They are intentionally standalone (no eager
imports here) so `-m` execution stays clean and fast.
"""
