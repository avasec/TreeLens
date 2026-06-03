# MIT License — TreeLens test harness support.
"""Make a skipped schema suite LOUD, not silent.

`tests/test_schema.py` validates the wire contract (../schema/) but needs the
OPTIONAL `jsonschema` dev-extra. When it is absent the suite skips cleanly so the
zero-dependency kernel can still be run with a bare `pytest tests/` — but a green
"N passed" then hides the fact that the contract was NOT validated this run.

This hook prints an unmissable banner in the terminal summary (the last thing on
screen) whenever — and ONLY when — the schema suite was skipped because the
validator is missing. A real failure stays red on its own; a real pass needs no
banner. See README.md / CONTRIBUTING.md "validating run" notes.
"""


def _jsonschema_present():
    try:
        import jsonschema  # noqa: F401

        return True
    except ImportError:
        return False


def pytest_terminal_summary(terminalreporter):
    """Emit a loud 'schema NOT validated' banner iff jsonschema is absent."""
    if _jsonschema_present():
        return  # schema suite actually ran — nothing to warn about

    line = terminalreporter.write_line
    sep = "=" * 70
    terminalreporter.write_line("")
    line(sep, yellow=True, bold=True)
    line("  ⚠  SCHEMA SUITE SKIPPED — the wire contract was NOT validated.",
         yellow=True, bold=True)
    line("     'jsonschema' (a dev-extra) is not installed, so schema/ was",
         yellow=True)
    line("     never checked this run. A green result here does NOT mean the",
         yellow=True)
    line("     contract is valid.", yellow=True)
    line("", )
    line("     Validate it:  pip install -e \".[dev]\" && pytest tests/",
         yellow=True, bold=True)
    line("     (On a pull request, CI installs the dev-extra and runs this",
         yellow=True)
    line("      check automatically as a hard gate.)", yellow=True)
    line(sep, yellow=True, bold=True)
