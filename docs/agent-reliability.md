# Agent reliability notes

## November 4 shell evidence

Project runs compare file hashes before and after shell commands, including failures,
timeouts and automatic checks. They use task review's file list without building a code
index or storing file contents. Interpreter writes, Git operations, and shell scripts
make earlier checks stale when they change project files. No-op writes and
timestamp-only changes preserve checks. Removing scratch files created in the current
run also preserves them.

Snapshots honor Git ignores and `.olithignore`, skip symlinks and common
dependency/build folders, and include binary and extensionless project files. Each
snapshot covers up to 20,000 files and hashes up to 64 MiB of new content. Unchanged
files reuse cached hashes. Incomplete or unreadable snapshots make earlier checks stale
and cannot verify a new check. Command parsing identifies checks and handles runs
without a project root. Snapshots see only the final state, so keep edits and tests in
separate commands when their order matters.

Investigated on October 17, 2026, after a slow SDL runner task repeatedly rewrote source
and produced broken gameplay. The original game stayed unchanged. Model repair checks
used temporary copies.

## Confirmed runtime bugs

- A refused rewrite was followed by `rm` and a full recreation of the file.
  Common delete, truncate, and replacement bypasses now return a structured
  rejection. Targeted edits still work.
- An automatic verification report was stored as a fake `run_shell` call.
  The model copied its internal label into the shell repeatedly. New reports
  use evidence attached to an actual tool result. Legacy fake calls are omitted
  from model replay, and the label cannot execute through the shell tool.
- Reused todo IDs inherited completions from earlier requests, while a later
  plan update could restart finished work. Plan state now follows user requests.
- Changing the plan changed the system prefix. The opening plan now stays
  stable throughout a run, with updates in tool results.
- Shell pipelines could report success when a test failed before `tail`.
  Bash tools now enable `pipefail`, and failed exits show failed activity.
  A command that deliberately overwrites its status with a later successful
  command can still hide failure, so direct test commands remain preferable.
- Identical C++ probes with different temporary filenames escaped the repeat
  guard. Their source, compiler arguments, and included project revisions now
  determine their identity. Editing source permits the same test to run again.
- Python checks using combined imports, such as `import json, unittest`, were
  not discovered. Test-framework discovery now reads Python import syntax.
- Shell commands could block the server event loop. They now run in a worker,
  so other requests remain responsive while a command is running. A stopped
  worker still finishes before the generation lock is released.

Successful simple project-local C/C++ compiler commands can also be saved for the source
they actually build. Manually saved commands take precedence. Commands with pipelines,
substitutions, or outputs outside the project are not learned automatically.

## What the game checks cover

The isolated harness advances 600 frames, checks increasing score and speed,
ground-obstacle collision, a clear jump, and pause behavior. The starting game failed
because its integer score increment rounded to zero each frame and its dinosaur
collision coordinates did not match obstacle coordinates.

Use `scripts/bench_game_repair.py --source /path/to/game.cpp --output
data/benchmarks/repair.json` with Olith's Python environment. This diagnostic harness
expects this SDL runner's `Game`, `Obstacle`, and `step` interface. It does not work
with arbitrary games. It saves timings, tool history, independent check results, and a
patch. The source argument and test harness must remain unchanged for a passing result.

These checks cover this repair task. They do not give a general coding score. A build or
window launch alone does not prove gameplay works, and better code search alone does not
prove better coding results.

## Verification results

The targeted tools, agent-loop, project-check, pairing, and integration suite passes 285
tests. The real VS Code extension host passes 24 checks, including live progress, editor
writes, undo, and pairing restoration.

Real model results are less encouraging. Qwen3.8 27B at the configured high effort
reached correct score and collision changes in one 24-tool probe, but the diagnostic
budget interrupted the last edit, leaving collision uncorrected. A fresh run with a
larger budget spent 240 seconds debugging and left the source unchanged. Qwen3-Coder 30B
also failed this task, producing a compile error and claiming success. That coder run
preceded the final test-discovery and repeat guard fixes, so it does not measure their
final behavior.

At this point, the full repair had not passed. These runs could not show that coding
quality or task speed had recovered. The independent checks caught the failures, and the
original source and test fixtures stayed unchanged. Qwen3.8 27B was restored after the
coder comparison. Runtime fixes require restarting Olith.

## October 31 harness changes

Saved chats showed runs of 296 and 340 tool calls with no thinking at all, the same file
read 196 times, and blocked rewrites. The loop now thinks on every step, allows rewrites
with backups, caps the run at 80 calls, caps verification bounces, and stops counting
re-reads of results the model can no longer see. VS Code edits reformatted on save now
count as written.

Same repair task, Qwen3.8 27B, high effort, five runs each:

| | Passed | Tool calls | Time |
|---|---|---|---|
| Before | 4 of 5 | 6 to 60 | 40 to 360s |
| After | 5 of 5 | 7 to 10 | 86 to 115s |

The failed run before the change looped on one shell command and gave up. Typical runs
are about 30 seconds slower because of thinking, but the slowest run went from 360s to
115s. Five runs is a small sample, and this task does not exercise rewrites or VS Code.

### Rerun of the 296-call request

`scripts/bench_dino_rerun.py` replays the original message, file and folder with the
user's own settings. Gameplay and build checks are hidden from the model and run
afterwards. Three runs each:

| | Calls | Time | Outcome |
|---|---|---|---|
| Before | 19 | 2 min | Build task fixed, gameplay changed beyond what the check can test |
| Before | 151 | 19 min | Hit the 150-call cap mid-loop, 126 shell calls |
| Before | 65 | 7 min | Gave up on repeated rejections, build task still broken |
| After | 23 | 5 min | Build task fixed, score slightly under the check's threshold |
| After | 29 | 12 min | All checks pass |
| After | 30 | 22 min | All checks pass |

No run after the change looped or was refused a repeat. Runs are slower: one generated
79K tokens, mostly thinking, and hit the reply limit five times. One run wrongly decided
SDL2_ttf was missing and replaced the font code.

### Live VS Code run

`scripts/ide_live_test.py` drives the real extension against a real backend with
format-on-save on. Two runs each on a small JavaScript fix. Before, every run reported a
false "Editor save did not match" error, and one then hit "Anchor text not found"
because the edit had already landed. Both still finished. After, no failed steps.

### Bigger thinking replies

One slow run lost about half its time to steps cut off at 8,192 tokens and retried
blind. A 24K limit plus carrying cut-off reasoning into the retry was compared against
the round 1 code, interleaved on the same machine:

| | Game repair, 5 runs | 296-call rerun, 3 runs |
|---|---|---|
| Round 1 | 5 of 5, avg 123s | 3 of 3 pass, 282 to 605s |
| 24K limit | 5 of 5, avg 99s | 1 of 3 pass, 383 to 787s |

The slow run was an outlier, so there was little waste to recover, and one step used the
full 24K thinking. The limit is back at 8,192. Carrying cut-off reasoning forward stays,
since it only changes what a retry sees. The failed reruns claimed "verified" from tests
the model wrote itself.

### Olith's record of a run

Failed runs said "verified" based on tests the model wrote itself, so Olith now keeps
its own record and shows it above the debrief. Same code either side, except for the
record, interleaved:

| | 296-call rerun | Build task fixed | Game repair |
|---|---|---|---|
| Before | 3 of 3 gameplay pass, 216 to 850s | 2 of 3 | 3 of 3, 104 to 139s |
| After | 4 of 4 gameplay pass, 254 to 810s | 4 of 4 | 4 of 4, 103 to 109s |

The missed build task said "All fixed and verified" over a broken task. Replayed over
every transcript, the record never claims an existing test on the project that has none,
and always does on the one that has. No run on the new code failed a hidden check, so
the warning has not yet been seen against a real bug.

The model still opens with "All fixed and verified" even with the prompt line asking it
not to. The record sits above that claim rather than preventing it. Every rerun got a
request to revise its final reply, which then listed extra changes, such as moved bird
heights and a self-test added to the game's source.
