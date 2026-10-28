# A killed run leaves a `.partial` staging directory behind forever

**Found by:** the black-box acceptance harness (`robot-runtime-assurance`), running
this repository only through its published CLI.

## What happens

`run_scenario` creates its staging directory with a `.` prefix and a `.partial`
suffix, then publishes it atomically with `os.replace`:

```python
staging = Path(tempfile.mkdtemp(prefix=f".{run_id}.", suffix=".partial", dir=output_dir))
...
except BaseException:
    shutil.rmtree(staging, ignore_errors=True)
    raise
```

The `except BaseException` handler cleans up on every error Python can raise. It
cannot run for `SIGTERM` or `SIGKILL`, because neither is translated into a Python
exception. The result is a directory such as:

```
runs/.normal-001-5c52031e9c64.0zs47fm2.partial/
```

that no code path ever removes.

## Reproduction

```bash
cd <workbench-checkout>
python3 -m pip install -e .          # or use the local import shim
cat > /tmp/sleep.py <<'EOF'
import time; time.sleep(60)
EOF

python3 tools/scripts/sim_cli.py run normal-001 \
  --runner external --command python3 /tmp/sleep.py \
  --output-dir /tmp/repro/runs &
sleep 2
kill -TERM %1                        # SIGKILL behaves the same way
wait
ls -a /tmp/repro/runs/
```

Observed on this checkout (commit `3737d30`, Python 3.12.3):

| Signal | Exit | Published artifact | `.partial` left on disk |
| --- | --- | --- | --- |
| `SIGINT` | 1 | `normal-001-…` (`INTERRUPTED`) | no |
| `SIGTERM` | -15 | none | **yes** |
| `SIGKILL` | -9 | none | **yes** |

`SIGINT` is already handled correctly: the runner terminates the process tree and
publishes an `INTERRUPTED` artifact. Only the two signals that bypass Python's
exception handling leave residue.

## Why it matters

- The directory is invisible to `--json` output and to every summary, so a user who
  runs `kill` on a stuck run has no way to learn that a run directory was left
  behind.
- It contains no `metadata.json`, so the existing provenance scanner correctly
  ignores it — which is exactly why it can accumulate unnoticed.
- An acceptance harness that reads this output directory cannot distinguish "this
  run published nothing because it was killed" from "nobody looked here", unless it
  enumerates dot-directories by hand. That is how the finding was produced.

## Fix applied

`run_scenario` now sweeps stale staging directories before creating its own:

```python
STALE_STAGING_SECONDS = 700  # longer than the 600 s maximum manifest timeout
_sweep_stale_staging(output_dir)  # removes `.*.partial` dirs older than that
```

The age guard is the important part: a `.partial` directory younger than the
longest possible run may belong to a concurrently executing process, so it is left
alone. Only directories that cannot be live are removed.

Covered by three tests in `tests/unit/test_sim_cli.py::StaleStagingTests`:

- a stale directory is removed by the next run in the same output directory;
- a fresh directory is left in place;
- a regular file whose name matches the pattern is ignored, not deleted.

## What this does not fix

A still-running process that is killed *between* two runs still leaves its
directory until the next run happens. Handling the signal itself would need a
`SIGTERM` handler that terminates the child process tree and publishes an
`INTERRUPTED` artifact, mirroring what the `SIGINT` path already does. That is a
larger change and should be decided separately: it changes the process's signal
semantics, so it belongs with the run-lifecycle owner (ADR-0009), not with a
cleanup fix.
