# How Olith works

These notes explain the main parts of Olith and the limits they use.

## System

| File | Job |
|---|---|
| `server.py` | FastAPI app, SSE streaming, the agent loop, context management |
| `agent.py` | Tool schemas and implementations, shell classification, plan state |
| `symbols.py` / `code_structure.py` | Symbol boundaries, bundled Tree-sitter grammars, syntax facts |
| `repo_index.py` | Saved project index, dependency maps, text and vector search |
| `project_commands.py` | Discover/save verification commands and preserve manual overrides |
| `task_changes.py` | Task baselines, change review, and guarded edit-block undo |
| `language_servers.py` | On-demand local LSP sessions, navigation, and diagnostics |
| `github_tools.py` / `integrations.py` | Git/GitHub operations and local integration APIs |
| `editor_bridge.py` / `extensions/olith-vscode/` | VS Code pairing and edits saved through the editor |
| `config.py` | Role tables, effort tiers, system prompts, defaults |
| `models.py` | Model probing, the registry, fit math, hardware |
| `library.py` | Browsing Ollama's library and Hugging Face's GGUF repositories |
| `catalog.json` | Curated model list, descriptions, hardware tiers |
| `ollama_client.py` | Ollama protocol, model loading, token estimation |
| `storage.py` | Chats, projects, memory, settings, canon |
| `browser.py` | Chrome over the DevTools protocol |
| `computer.py` | Screenshots, desktop input, element locating |
| `guards.py` | Editing rules shared by the browser and desktop tools |
| `connections.py` | Accounts Olith is allowed to act in |
| `static/app.js` | Chat UI, models and settings |
| `static/browser_connections.js` / `static/tool_connections.js` / `static/integrations.js` | Connection groups, service setup and editor status |
| `desktop.py` | pywebview shell that hosts the server |

Inference and indexing run locally. GitHub, web search, and browser features contact
outside services when used.

## The agent loop

The loop runs a tool call, gives its result back to the model, and continues. A normal
reply ends the turn. The default tool budget is 80 calls; repeat and stall guards handle
runs that stop making progress.

- Repeated calls get the tool's rejection details so the model can change its approach.
- Replies ending in a promised action get a retry instead of ending the task.
- A final reply with open plan items gets two reminders, then is accepted.

`todo_write` holds the plan. One item completes per call and it has to be the active
one, or a run can mark work done that never happened. Within the same user request, an
update cannot reopen an unchanged completed milestone. New requests can reuse milestone
IDs without inheriting old completions. A resume keeps the existing plan. The system
prompt holds the opening plan snapshot; later changes arrive through actual tool results
so they preserve the KV prefix.

The repeat guard recognizes identical C/C++ probes even if the model renames them,
whether they live in the project or in a temp folder, and whether or not they include
project source. Changed probe bodies, compiler flags, or included project source get a
fresh attempt. Successful source edits also reset call counts so the same test command
can run again against each new revision.

Automatic verification reports are evidence, never synthetic shell calls. Failing checks
go back to the model for repair. Passing checks accept the final reply without an extra
model round. The final reply includes the actual check summary, including failures or
commands not run. Existing chats omit the old synthetic verification command when
rendering history.

`evidence.py` keeps Olith's record of changes and checks and shows it above the model's
final reply. It separates existing tests, tests changed during the task, and build/run
checks. A reply that contradicts that record or omits a changed definition gets one
retry. Later turns replay the model's reply without the record. Before/after file hashes
catch shell edits too, including interpreter writes and failed commands.

`write_file` can replace an existing file. Every replaced file is backed up first, and
task review can undo it. Emptying a nonempty file through `edit_file` is still refused.
New-file writes and append chunks keep their 12,000-character limit.

Agentic runs think on every step when the effort tier allows it. If a thinking step runs
out of room, its reasoning is carried into the retry. The reply limit stays at 8,192
tokens (`agent_thinking_reply_tokens`); 24K was tested and was no faster. A run gets 80
tool calls (`agent_tool_budget`), then writes a debrief with tools withheld. Failing
checks can send a debrief back at most three times. A call whose earlier result has left
the prompt is not counted as a repeat. Replayed tool calls carry their real arguments,
since a placeholder in their place got copied into new writes.

## Context

Settings hold a context window for each role, with optional per-model overrides. Agentic
needs more history than Chat. Limits stay within what the model and GPU can handle.

The numbers below are the default 27B on a 24 GB card, which is the budget the rest of
the design was built around.

| `num_ctx` | KV cache | Total resident | Notes |
|---|---|---|---|
| 32768 | ~2.9GB | ~18.7GB | Fits, too small for agentic work |
| 65536 | ~5.8GB | ~21.1GB | The agentic default |
| 131072 | ~11.7GB | ~27.4GB | Spills to system RAM |

System prompt and tool schemas cost about 6.8k tokens. At 32768 that leaves ~7k of
history, under two tool results, which is where reread loops come from. 65536 leaves
~20.7k. Chat and reasoning answer once, so they stay at 32768. `num_ctx` only ever
grows, because Ollama reloads the model whenever it changes.

All 14 tool schemas cost about 4.2k tokens and their prompt guidance another 1.2k.
Nothing is trimmed at the agentic default. Below roughly 16k the optional tools are
dropped in a fixed order, and the prompt is trimmed with them so it never names a tool
that is not there.

### Tool lists by model size

Model size does not change the base tool list. Size-based changes performed worse in the
benchmark.

`policy.py` sorts a model into `tiny` / `small` / `mid` / `full` by parameter count, and
`apply_policy` can cut the tool list, reorder it, or shorten every description.
Benchmarked on five models against each one's own baseline:

| Dial | Result |
|---|---|
| fewer tools | worse on every model tried, -2 to -7 prompts |
| shorter descriptions | +4 on llama3.2:3b, -2 on two others |
| priority ordering | -5 combined, against a control that repeated exactly |

Cutting tools removes capability and buys no better choices among what is left. The
smallest model stops answering rather than choosing better. Shortening descriptions
helped exactly one model, which was the model it had been tuned against.
`qwen2.5-coder:7b` scores better untouched than anything else measured, large or small.

All three options are off by default and covered by tests. Keep the benchmark available
for future model comparisons.

Trimming schemas that do not fit a small context window is a separate thing, it predates
this, and it still happens.

### Tool name aliases

Small models reach for the tool name they learned in pretraining rather than ours, so
`execute_command`, `str_replace`, `grep` and about thirty others resolve to the real
tool on the way in. A model emitting the real name never takes that path, so this cannot
change what a working model does.

### Reading by symbol

`read_file` takes `symbol` to return one function or class, and `outline` to list what a
file defines, so a model does not have to guess line numbers. Python is parsed with
`ast` and has exact boundaries. JavaScript, TypeScript/TSX, Go, Rust, Java, C and C++
use bundled Tree-sitter grammars. A definition containing parse errors is marked
inexact. If grammars are missing, JS/TS retains its brace-counting fallback with `exact:
false`. Unsupported languages fall through to a line read. A name that is not there
comes back with the names that are.

These options share `read_file` to avoid another tool schema in the prompt.

`scripts/bench_tool_choice.py` measures tool choice against real models, one variant at
a time. Comparisons are only valid inside a single process.

### Persistent project navigation

`repo_index.py` stores one SQLite database per resolved project root under
`data/indexes/`. It caches definitions, signatures, identifier locations, import
targets, source hashes, and bounded source chunks. The cache keeps navigation facts
without keeping whole syntax trees. Tree-sitter grammars are installed with the app, so
parsing never downloads a language at runtime.

The manifest refreshes when an agent run or indexed search starts. Unchanged files reuse
their cached facts. Changes to size, mtime, or ctime trigger a hash check; changed
source is reparsed and its old chunks/vectors are invalidated. Deleted or newly excluded
files are removed. Parser-version changes invalidate the syntax cache. Results verify
the source hash before returning a location; source edited during a query is omitted
rather than presented as current.

Git projects use `git ls-files` for tracked and unignored new files. Outside Git,
pathspec applies nested `.gitignore` rules. `.olithignore` adds project-specific
exclusions even for tracked files. Common dependency/build/model folders, symlinks,
known secret filenames, binary files, and files over 1 MB are skipped. The inventory is
capped at 20,000 eligible files and reports partial coverage. These filename exclusions
are not a secret scanner. The cache contains source snippets and belongs in the private
`data/` directory with the user's chats.

`search_files` keeps regex as its default, adding four modes without another tool
schema: `hybrid`, `symbols`, `references`, and `map`. Hybrid search combines SQLite
FTS5/BM25 and exact-symbol matches with optional cosine similarity using reciprocal rank
fusion. Source chunks follow declaration boundaries where possible, then split at 60
lines / 2,200 characters. Results share a 6,000-character source budget so a search
usually fits in the normal tool result. Text formats without a grammar can still
participate in lexical and semantic retrieval.

The graph connects files through locally resolved import targets and uniquely owned
identifier names in the same language family, excluding Python builtins and short common
names. Personalized PageRank prioritizes dependencies, while direct task matches keep an
obscure relevant file visible. The map is bounded by the existing token estimator,
defaults to at most 1,400 tokens, and shrinks with the model's context window. It is
computed once per run, including each delegation, then passed explicitly to message
rendering. Edits do not rewrite the map mid-run and invalidate Ollama's prompt cache. A
new run gets a fresh map.

Identifier occurrences include declarations and are not compiler-resolved references.
Import aliases, dynamic imports, reflection, and language-specific module resolution can
leave graph edges incomplete. Neither the graph nor the parser is a safe rename/refactor
engine; the agent must inspect source and test.

### Optional local embeddings

Settings enables semantic search and selects an already installed Ollama embedding
model. CPU is the default (`num_gpu: 0`); automatic device selection is optional.
`/api/embed` batches eight chunks with truncation disabled and unloads the embedding
model afterward (`keep_alive: 0`). Nomic models receive their query/document prefixes.
Vectors are validated, normalized, and stored locally in SQLite; search uses an exact
cosine scan with a bounded top-results heap rather than requiring another database
service.

The cache identifies vectors by model tag, Ollama weight digest, and input format
version. Replacing a model tag cannot mix its old vectors into a query. Missing models,
service failures, and incompatible vectors leave lexical search available and report the
reason. Normal Windows/Linux setup includes the embedding model through the shared
catalog utility list, verifies its presence, and enables semantic search only when that
setting has not already been saved. Custom wizard choices include utilities too.
Explicit model-only maintenance choices and `--skip-models` keep their requested scope.
Runtime search never downloads weights. Embedding-only models cannot serve chat,
agentic, or titles.

The project Index panel refreshes syntax, builds embeddings, shows progress, stops after
the current batch, and searches the index. Embedding jobs share the generation lock with
chat, releasing it between batches. They wait while an agent runs. Semantic queries and
successful agent replies enqueue incremental embedding refreshes, so source changes are
picked up on use without a permanent filesystem watcher. A cancelled query waits for its
in-flight embedding worker before releasing the model lock. Large projects may benefit
from an approximate vector index later; the current exact scan should be benchmarked
before raising the inventory bound. No retrieval-quality or model-speed improvement is
assumed without an evaluation against representative tasks.

### Autonomous project tools

Opening a project's tree, starting a parent agent run, and verifying its changes
discover commands from the indexed file manifest. Explicit npm/pnpm/yarn/bun scripts,
Python test imports/configuration, packaging configuration, Makefile targets, Cargo.toml
and go.mod supply evidence. Watch-mode and npm placeholder tests are excluded. The
command list persists under `data/project-commands/`, keyed by resolved root. Manual
overrides replace discovered entries and survive configuration changes. If no build or
type-check command is found, leave it unset.

Each parent agent run captures a task baseline of eligible indexed source under
`data/changes/` before the first model sample. It includes the starting working tree
rather than Git HEAD, so pre-existing uncommitted edits remain the baseline. Compressed
SQLite snapshots are bounded at 64 MB; omitted original paths are tracked so partial
coverage cannot mislabel old files as newly created. Unchanged baseline rows are pruned
when the run finishes. This records concurrent source changes during the task too; it is
a time-based review, not proof of edit authorship.

When the model attempts its final reply and source differs from the baseline, Olith
rediscovers commands and runs applicable checks. Results are appended as a tool/result
pair before another model sample, so the final answer can account for failures and the
agent can fix them. A content revision is verified once; another successful edit permits
another check. Documentation-only and read-only work skip commands. Commands use the
existing shell approval mode, including checking package-script bodies before automatic
execution. Checks requiring approval are reported as unexecuted and remain available
through the normal run_shell approval flow. Default checks have a 120-second timeout,
with manual overrides up to 600.

The run wrapper saves its final review on completion, interruption, or error. Review
shows individual SequenceMatcher edit blocks. Undo checks the selected blocks, project
root, symlinks and full file hashes before applying inverse edits. It refuses files
changed since review. Created/deleted files are supported. Undo never touches the Git
index or uses reset/checkout. Other tasks cannot undo through the API while generation
holds the shared lock, and stale change-set IDs are refused.

Local language servers are reused per root/language, with four idle sessions retained
and older idle processes closed, and synchronized with disk through
didOpen/didChange/didSave, using versioned diagnostics. Initial analysis gets a bounded
wait before navigation, since TypeScript can initially resolve a dependency only to its
import binding. Definition, references, hover, and diagnostics and rename previews
extend read_file without adding a tool. Indexed reference searches automatically prefer
server-resolved results when a unique definition is found; otherwise their heuristic
fallback remains explicit. Final verification gathers diagnostics for up to eight
changed files. Missing/incomplete analysis is reported instead of being called a clean
pass.

Rename uses the server's textDocument/rename operation to return proposed text edits
with source revisions and UTF-16 positions. The client does not apply workspace edits,
and out-of-project locations or file operations are reported as skipped. The agent
inspects and applies accepted proposals through edit_file.

The installer bundles python-lsp-server, Pyflakes, and Rope through pip; when Node 18+
is present it provisions TypeScript 5 / typescript-language-server 4 locally in
data/tools/language-servers. These versions keep older supported Node installations
working. Existing project/server installations can take precedence. Pyright, gopls,
rust-analyzer, clangd, and jdtls are discovered when installed. Servers start on demand,
cannot request workspace edits or execute commands through this client, and locations
outside the active project are discarded. Shutdown closes their processes. Python's
bundled server provides Jedi navigation and Pyflakes diagnostics; complete Python type
checking requires an appropriate configured checker such as Pyright or a saved project
type-check command.

### Shrinking the prompt

Messages are never deleted on disk. Olith inserts a marker message and changes what
renders after it. Two levels:

- Drop old tool calls and their results, keep the narrative. Fires around 50k
  and lands around 27k.
- Summarize everything before the marker into Objective, Done, Files, State,
  Next, so the model knows what came before it and where it left off.

Dropping old detail keeps the newest read of each file that has not been edited since,
up to 20% of the window. Without that, the model lost the file it was fixing and read it
again in a loop. A failed sample only narrows the replay when the prompt fills over half
the window, since the retry already gets more room.

The latest user message is repeated verbatim after a summary, since a summary
paraphrases and the model checks its work against the literal request. Tool results too
big for the prompt go to disk and appear as head, tail and path.

Because the prompt only grows between markers, Ollama reuses its KV cache and a step
costs only its new tokens. That is why per-step instructions ride on the newest tool
result instead of a system message: Qwen's template moves system messages to the front,
so a trailing instruction lands in the prefix and voids the cache. Measured at 33.8s for
seven tokens against 3.5s for 3,101 appended normally.

If you profile this, `prompt_eval_count` reports the full prompt length whether or not
the cache was reused. Only `prompt_eval_s` tells you anything.

### Token estimates

Code and tool JSON need about 2.5 times as many tokens per character as prose. A flat
four-characters-per-token estimate can overflow context and make Ollama drop the start
of the prompt. The estimator accounts for content type, leaves some room and learns a
correction from each chat's actual token counts.

## Models

Olith uses models installed through Ollama. Each model has capabilities and the roles
the user enabled.

| Role | Where it appears | Requires |
|---|---|---|
| `chat` | Chat tab picker | nothing |
| `reasoning` | The chat model with thinking on | a thinking mode |
| `agentic` | Agentic tab picker | tool calling |

One model per job, with no swapping mid-run. One model can hold every role, which is
what a single-model install looks like. The defaults are Qwen3.8 27B for chat and
computer use and Qwen3-Coder 30B for agentic work, because the mixture of experts emits
tool calls far faster than a dense model its size.

### Model libraries

Ollama and Hugging Face share the same model browser and install flow.

| | Ollama | Hugging Face |
|---|---|---|
| Listings | About 240 curated models | Thousands of GGUF repos |
| Fetching | Parse and cache the library page for a day | Fetch pages as needed |
| Paging | Offset into the cached list | Provider cursor |
| Each row | Model family and sizes | Repo and quantizations |

Ollama imports Hugging Face GGUFs through `hf.co/` tags. After the download, both
sources use the same probing, role selection and model loading code. Keep Hugging Face
tag capitalization and do not add `:latest`; its suffix names a quantization.

Rows show available GGUF metadata before download: tool support, missing chat templates,
gated access, short context and vision-projector limits. These are preliminary warnings.
`/api/show` supplies the installed model's capabilities.

The version picker shows sizes, context limits and which versions fit. Put the largest
fitting quantization first. Installed versions stay visible, and users can still install
another size.

### Matching models across libraries

Match GGUF architecture and parameter count to warn about models already installed from
the other source. Use bounded name matching when metadata is missing: `qwen3.8` matches
`Qwen3.8-27B-GGUF`, but `qwen3` does not match `qwen3.8` or `qwen3-coder`.

Call this a match, not proof of identical files. Fine-tunes can share the same
architecture and size.

### Capabilities come from Ollama

`/api/show` reports what a model can do, so there is no compatibility table to maintain:

```
qwen3.8:27b      capabilities: ['completion', 'vision', 'tools', 'thinking']
qwen3-coder:30b  capabilities: ['completion', 'tools']
```

`think: true` on a model without a thinking mode is a hard 400 from Ollama, so the
capability gates the request, and the client drops the key and retries if a stale
profile gets it wrong. Tool calling gates the agentic role: a model without it cannot be
assigned there, and the toggle is disabled with the reason on it. Vision gates computer
use.

`model_info` supplies the rest: the native context length, and the block and head counts
that size the KV cache.

### What fits

```
KV bytes per token = block_count * head_count_kv * (key_length + value_length)
```

At `q8_0` that is a byte per element. Use this for ordinary attention. For hybrid
models, use measured catalog values when available. Profiles say which they used.
Qwen3.8 27B reads 8.1 GiB at 64k computed against 5.8 GB measured, and the computed
figure would refuse a 64k window on the card Olith was built for.

### Effort levels

Thinking runs only when both the effort tier and model support it. Each tier's hint says
whether thinking is on, off or unavailable for that model.

### Calls written in reply text

Some models write a tool call into the reply body. Recover it only when it names a
registered tool and its arguments are an object. Unmarked JSON must reach the end of the
reply so an example quoted in the middle is not executed.

If the call cannot be parsed, report the attempted tool name and ask for a retry. Never
guess arguments. Malformed-call retries have their own limit.

## Delegation

`delegate` runs a sub-task on a fresh transcript, so its reading costs the parent only
the summary it hands back. Sub-agents cannot write files, so a delegation can always be
re-run.

Only offered on large projects. The schema costs 324 tokens on every request, and on a
small project there is no context pressure for a fresh window to relieve.

## Computer use

`computer` returns a screenshot after each action. The model sees that result before
choosing its next move. X11 uses XTest and Windows uses SendInput, both through
`ctypes`.

Screenshots have a maximum long edge of 2000 px. The tool maps model coordinates back to
the display. A `region` request returns a crop at native resolution for small text.
Frames scaled below 60% warn that body text may be unreadable. Only the latest two
frames keep their pixels in the prompt and saved chat.

The screen-change check compares a 48×27 brightness grid, ignoring small changes such as
cursor blinks. Change counters are part of repeat-call signatures, so a repeat click can
run after the screen changes. Four actions in a row that leave the screen unchanged stop
the run, even when their coordinates differ.

### Editing guards

`guards.py` shares rules and selection state between browser and desktop tools.

- After select-all, replacing keys or typing require
  `confirm: "replace_selection"`. Formatting shortcuts still work.
- Only keyboard actions clear the selection flag. Toolbar clicks can leave the
  document selected.
- More than four Return presses without text between them are refused.

Document prompts tell the model to work top to bottom and set paragraph styles before
typing. Input results report what was sent; they cannot prove that an app accepted every
character.

### Action batches

A desktop step measured 15.5 seconds, with only 0.7 seconds spent on the action. The
`actions` list runs a sequence with one screenshot at the end to reduce model round
trips. Stop at the first refused action and report which steps ran.

### Finding controls

Use these methods in order:

1. Browser refs from `snapshot`, with `click`, `fill` and `read`. Recheck the
   target's box and try several points before reporting it covered. Insert text
   through the browser's input API.
2. Native accessibility refs from `elements`. Drop boxes outside their window.
3. Screenshot coordinates when no named control is available.

Chrome uses a separate profile under `data/browser`, keeping the user's normal browser
separate. Users sign in themselves. Password fields are refused. If Chrome is already
listening on the configured port, attach to it and keep the driven tab fixed.

### Repeated observations

Observations skip the duplicate-call guard because the page may have changed. A separate
guard stops repeated looking without acting: after five observations, the model gets a
correction. An action resets that count. Desktop frame IDs and browser page counters
tell the loop when each tool changed the screen.

### The model

Grounding is a separate skill from vision. qwen3.8 can read a toolbar and still not say
where its Bold button is. When nothing has been chosen, computer use prefers a model
trained to point, the qwen3-vl family, over a general one, and a model the user picked
still wins. qwen2.5vl is deliberately not on that list, because Ollama reports it
without tool calling, so it can see a screen and never act on one.

While a run uses the desktop, a click-through border and action label show what Olith is
doing. Try GTK first and Tk as a fallback. Return the actual error if neither can draw
the overlay.

## Connections

Browser sessions, API keys, CLIs, MCP tools, GitHub and VS Code share Connected and
Disconnected groups. Any working connection puts its service in Connected. A verified
CLI does not need a website login. Paused tools and offline editors count as
disconnected unless another connection for that service still works. The panel refreshes
status while open.

Browser sign-in starts at `/connect/{id}`, which explains why Olith opened the window.
Chrome's own sign-in and sync are disabled, and Olith keeps control of the chosen tab.
The user signs in; session cookies stay in that Chrome profile. Only provider-specific
protected URLs can confirm login automatically. Otherwise, the user confirms the
session. A public homepage is not proof of login.

API keys, tokens and MCP secrets stay in the OS keyring. Report an unavailable keyring
instead of saving them beside the code. The model sees account names and available
access, never secret values. `KEY_CONSUMERS` lists the tools that can use saved keys;
other keys are described as stored but unavailable to tools. Browser sessions need
computer use enabled in that chat.

CLI connections reuse the provider's own login and verify it with an account or API
check. MCP connections verify the server and discover its tools. Browser sign-in does
not grant either kind of direct access. Connected service schemas are discovered as
needed through `connection_tools`; `connection_call` runs one. Sub-tasks do not inherit
these tools.

A browser tool that reaches a login page reports an expired session to the model. It
does not change the saved status from one page load. Setup, observation and explicit
checks handle saved status. See [connections](docs/connections.md) for provider setup
and limits.

## File and shell safety

Reads and writes are limited to registered projects, Olith's folder, Documents, Desktop,
and uploads.

`edit_file` matches surrounding text instead of line numbers. Ambiguous anchors are
refused, and a failed one gets the closest real lines back so it can be fixed in one
attempt.

`write_file` can replace an existing file and saves a backup under `data/backups/`
first. New files and append chunks have a 12,000-character limit. `edit_file` refuses
edits that empty a nonempty file.

Supported file types are syntax-checked before saving. Inline HTML scripts map
JavaScript errors back to the HTML file's line numbers. Broken script tags cannot skip
validation.

Approval modes live in Settings:

- **Ask before every command:** confirm every shell command.
- **Balanced:** run ordinary commands; review destructive commands, privilege
  changes, system changes and downloaded code. GitHub and service changes ask too.
- **Shell only:** skip shell approvals; keep GitHub and service approvals.
- **Full access:** skip shell, GitHub and service authorization prompts. This is
  an explicit opt-in with a risk warning and a visible indicator while enabled.

File access remains a separate setting. Full access does not skip connection, schema or
file-revision checks. Legacy unattended-shell settings do not turn it on.

Shell review uses `shlex` to check programs, including commands passed through `find
-exec` and `xargs`. Remembered approvals cover every program that needs review, not just
a leading `cd`.

## When the agent needs you

Approvals and clarifying questions share one bar above the composer. Several stack and
page through, and a multi-part question gets tabs.

The server holds the open question keyed by chat id, and the UI polls
`/api/chats/{id}/pending` while a run streams. That polling is the recovery path.
Without it, a question whose event never reached the browser leaves the run stalled
until it times out with nothing on screen. Sub-agent questions are filed under the
parent chat's id.

## GitHub and VS Code integration

`github_tools.py` implements local Git with argument arrays and a fixed set of GitHub
REST operations. `git_status` keeps its original no-argument behavior; optional
action/options extend it without another tool schema. Unless Full access is enabled,
changes use the approval prompt, including in delegated runs. Execution happens in a
worker. Desktop Git actions explicitly confirm and hold the generation lock. Local Git
requires the registered root to equal the repository root; pathspecs are literal and
staging accepts explicit root-local files. Pulls are fast-forward only. Conflicts and
rejected pushes return failure output without discarding the working tree. PR merge
requires a reviewed exact head SHA. `create_repo` with `publish` adds origin and pushes
under one action. It checks for a commit and a free origin first, so a refusal leaves no
empty repo on GitHub.

GitHub API credentials are either `github-api` in the existing OS keyring or the
existing `gh` CLI login. API calls have a fixed api.github.com origin and do not follow
redirects. HTTPS Git uses a temporary token-free askpass script, with the credential in
the subprocess environment; the helper only answers github.com prompts. Credentials are
redacted from results and never passed as model/tool arguments. SSH remains the user's
SSH setup. GitHub.com is the supported host; merge queues, enterprise hosts, force
pushes and hard resets are out of scope.

`integrations.py` registers desktop Git/GitHub routes and editor routes. The existing
loopback Host and browser Origin restrictions remain in force. Pairing codes are one-use
and expire in two minutes. Editor pairings bind to a resolved root and authorize only
that pairing's chats. VS Code keeps the bearer token in SecretStorage. Olith persists
its SHA-256 hash, pairing ID, and root in an atomic, owner-readable
`data/editor-pairings.json` file. Pairings survive restarts and have no inactivity
timeout; explicit disconnect removes the saved record. Heartbeats and pending edit
proposals stay in memory, so restart never replays a stale write. VS Code retries
temporary outages automatically. No model receives the token. The pairing panel counts
down locally and polls code status to clear consumed or replaced codes. Expiry does not
generate a replacement or end a paired session. The desktop enables pywebview downloads
before window creation so VSIX responses open the native save dialog instead of being
cancelled by the default setting.

`editor_bridge.py` carries edit proposals from a worker to the extension. A thread-local
context surrounds each tool execution, including delegated file tools. It captures the
source before preparation, validates the candidate as usual, then queues a
revision-hashed proposal. The worker waits for the editor instead of writing disk. The
extension polls while chat streams independently, claims an edit, rejects
traversal/symlinks, stale disk revisions and dirty buffers, applies WorkspaceEdit,
saves, and acknowledges. The backend checks the resulting bytes before reporting
success. Disconnect/refusal/timeouts never fall back to direct writes. Stop cancels
unclaimed edits; claimed saves finish before the final task snapshot. Shell tools,
checks, Git and desktop selective undo retain their filesystem behavior and are not
editor-mediated.

`extensions/olith-vscode/` is a dependency-free local extension: webview sidebar,
selection context command, diagnostic CodeAction provider, native approval and question
dialogs, Stop, and task diff viewing. Workspace trust and local file schemes are
required; remote workspaces are rejected. Webview content is text, with a nonce CSP and
no backend network access from its renderer. The extension host owns authenticated
requests and streaming SSE parsing. A Python packaging script emits
`dist/olith-vscode.vsix`, also downloadable from the desktop UI. Node unit tests cover
the client/guards; `test/host.js` verifies real editor edits, undo and diagnostics.
`tests/ide_smoke_server.py` supplies isolated deterministic inference for an end-to-end
extension host run through the real agent tool loop.
