"""Allow `python -m codex_lcm ...` invocation (used by hooks + worker)."""

from .cli import main

if __name__ == "__main__":
    main()
