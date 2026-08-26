"""Module entry point: ``python -m agent.sweforge``.

Exists so the CLI is reachable without users reasoning about PYTHONPATH or
remembering the module path to ``cli``.
"""

from agent.sweforge.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
