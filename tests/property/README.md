# Property suites

Seeded, dependency-free property suites for the fail-closed boundaries named by
Issue #89. See `docs/qa/property-and-mutation-gates.md` for the design, the
evidence artifacts and the exit-code contract.

Run them directly:

```bash
python3 -m pytest tests/property -v
```

Or through the gate, which archives the corpus summary:

```bash
make property-gate
```

`_generator.py` holds the splitmix64 stream, the shrinker and the corpus
accounting. Each `test_property_*.py` file declares one logical `SUITE` name; the
gate keys the archive on that name rather than on the file name, so a rename
cannot silently drop a suite from the gate.

The suites are written to run inside a throwaway copy of the repository as well as
in place, because the mutation gate executes them with exactly one rejection
neuters. That is why each file inserts the local package paths instead of relying
on the editable install.
