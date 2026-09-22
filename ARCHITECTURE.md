# How Aether works

The numbers in this codebase look arbitrary. This is where they come from, so
read it before changing them.

## System

| File | Job |
|---|---|
| `server.py` | FastAPI app, SSE streaming, the agent loop, context management |
| `agent.py` | Tool schemas and implementations, shell classification, plan state |
| `config.py` | Role tables, effort tiers, system prompts, defaults |
| `models.py` | Model probing, the registry, fit math, hardware |
| `library.py` | Browsing Ollama's library and Hugging Face's GGUF repositories |
| `catalog.json` | Curated model list, descriptions, hardware tiers |
| `ollama_client.py` | Ollama protocol, model loading, token estimation |
| `storage.py` | Chats, projects, memory, settings, canon |
| `browser.py` | Chrome over the DevTools protocol |
| `computer.py` | Screenshots, desktop input, element locating |
| `guards.py` | Editing rules shared by the browser and desktop tools |
| `connections.py` | Accounts Aether is allowed to act in |
| `static/app.js` | The whole UI |
| `desktop.py` | pywebview shell that hosts the server |

Everything runs locally. The server talks to Ollama on 127.0.0.1 and nothing
leaves your computer unless you turn on web search.

## The agent loop

Ollama's native tool-calling contract, same as Codex and Claude Code. A tool
call means execute it and come back, a message without one ends the turn. There
is no step limit.

Three checks catch a run that has stopped making progress, and all three let it
finish rather than killing it.

- **Repeated call.** The model is told what the tool rejected, so it knows what
  to stop repeating.
- **Narration.** A reply ending on a promise ("Let me start by creating
  index.html") is a failed action. Length cannot tell a plan from a deliverable,
  so the check reads the last 300 characters for one.
- **Early debrief.** A final answer with plan items still open gets nudged
  twice, then accepted. Refusing it outright deadlocks a model that has
  finished.

`todo_write` holds the plan. One item completes per call and it has to be the
active one, or a run can mark work done that never happened. If a `merge:false`
rewrite comes back having forgotten every completion, they get restored, because
Qwen re-emits the whole plan mid-run with everything reset to `pending`. A
rewrite that keeps any completion is left alone.

## Context

The window belongs to the job, not the model. The same model needs more history
agentically than it does in chat. Settings hold one window per role, a model can
override it, and both are clamped to what the model and the card can serve.

The numbers below are the default 27B on a 24 GB card, which is the budget the
rest of the design was built around.

| `num_ctx` | KV cache | Total resident | Notes |
|---|---|---|---|
| 32768 | ~2.9GB | ~18.7GB | Fits, too small for agentic work |
| 65536 | ~5.8GB | ~21.1GB | The agentic default |
| 131072 | ~11.7GB | ~27.4GB | Spills to system RAM |

System prompt and tool schemas cost about 6.8k tokens. At 32768 that leaves ~7k
of history, under two tool results, which is where reread loops come from. 65536
leaves ~20.7k. Chat and reasoning answer once, so they stay at 32768. `num_ctx`
only ever grows, because Ollama reloads the model whenever it changes.

All 14 tool schemas cost about 4.2k tokens and their prompt guidance another
1.2k. Nothing is trimmed at the agentic default. Below roughly 16k the optional
tools are dropped in a fixed order, and the prompt is trimmed with them so it
never names a tool that is not there.

### Tool surface by model size: tried, measured, rejected

Every model gets the same 14 schemas. Giving smaller models a smaller surface is
an obvious idea and it did not survive measurement.

`policy.py` sorts a model into `tiny` / `small` / `mid` / `full` by parameter
count, and `apply_policy` can cut the tool list, reorder it, or shorten every
description. Benchmarked on five models against each one's own baseline:

| Dial | Result |
|---|---|
| fewer tools | worse on every model tried, -2 to -7 prompts |
| shorter descriptions | +4 on llama3.2:3b, -2 on two others |
| priority ordering | -5 combined, against a control that repeated exactly |

Cutting tools removes capability and buys no better choices among what is left.
The smallest model stops answering rather than choosing better. Shortening
descriptions helped exactly one model, which was the model it had been tuned
against. `qwen2.5-coder:7b` scores better untouched than anything else measured,
large or small.

So every dial ships off, and a test pins that none can be turned on without
re-running the benchmark. The machinery stays so the question can be re-asked on
a model we have not seen.

Trimming schemas that do not fit a small context window is a separate thing, it
predates this, and it still happens.

### Tool name aliases

Small models reach for the tool name they learned in pretraining rather than
ours, so `execute_command`, `str_replace`, `grep` and about thirty others
resolve to the real tool on the way in. A model emitting the real name never
takes that path, so this cannot change what a working model does.

### Reading by symbol

`read_file` takes `symbol` to return one function or class, and `outline` to
list what a file defines, so a model does not have to guess line numbers. Python
is parsed with `ast` and is exact. JavaScript and TypeScript are matched by
brace counting and are marked `exact: false`. Anything else falls through to a
line read. A name that is not there comes back with the names that are.

This extends `read_file` rather than adding a tool, because a fifteenth tool
would cost more than the feature is worth.

`scripts/bench_tool_choice.py` measures tool choice against real models, one
variant at a time. Comparisons are only valid inside a single process.

### Shrinking the prompt

Messages are never deleted on disk. Aether inserts a marker message and changes
what renders after it. Two levels:

- Drop old tool calls and their results, keep the narrative. Fires around 50k
  and lands around 27k.
- Summarize everything before the marker into Objective, Done, Files, State,
  Next, so the model knows what came before it and where it left off.

The latest user message is repeated verbatim after a summary, since a summary
paraphrases and the model checks its work against the literal request. Tool
results too big for the prompt go to disk and appear as head, tail and path.

Because the prompt only grows between markers, Ollama reuses its KV cache and a
step costs only its new tokens. That is why per-step instructions ride on the
newest tool result instead of a system message: Qwen's template moves system
messages to the front, so a trailing instruction lands in the prefix and voids
the cache. Measured at 33.8s for seven tokens against 3.5s for 3,101 appended
normally.

If you profile this, `prompt_eval_count` reports the full prompt length whether
or not the cache was reused. Only `prompt_eval_s` tells you anything.

### Token estimates

Code and tool JSON tokenize about 2.5x denser than prose, so a flat 4 characters
per token underestimates. Underestimating is the expensive way to be wrong:
compaction fires late, the prompt overflows, and Ollama cuts it from the front,
dropping the system prompt and the plan. The estimator adjusts for density,
learns a correction per chat, and only ever guesses high.

## Models

Any model Ollama can run. Aether does not know about models, it knows about
three jobs, and every installed model carries the roles the user turned on.

| Role | Where it appears | Requires |
|---|---|---|
| `chat` | Chat tab picker | nothing |
| `reasoning` | The chat model with thinking on | a thinking mode |
| `agentic` | Agentic tab picker | tool calling |

One model per job, with no swapping mid-run. One model can hold every role,
which is what a single-model install looks like. The defaults are Qwen3.8 27B
for chat and computer use and Qwen3-Coder 30B for agentic work, because the
mixture of experts emits tool calls far faster than a dense model its size.

### Two libraries, one install

Models are browsed from a full screen with two sources behind one scroll.

| | Ollama | Hugging Face |
|---|---|---|
| Size | ~240 curated models | tens of thousands of GGUF repositories |
| Fetched | the library page, parsed and cached for a day | queried live, paged by cursor |
| Paged by | offset, over the local copy | Hugging Face's own opaque cursor |
| A row is | a family, with its parameter sizes | a repository, with its quantizations |

Hugging Face is a *source*, not a backend. Ollama imports GGUF directly from a
tag beginning `hf.co/`, so a Hugging Face model is pulled, probed, assigned and
run by exactly the code that handles an Ollama one. Nothing downstream of the
pull knows where a model came from.

Two rules the Ollama tag grammar does not share. Hugging Face tags keep their
capitals, because Ollama stores the repository's own spelling and folding the
case stops an installed model matching itself. And they never get `:latest`,
because the part after the colon is a quantization, so inventing one gets `model
not found` from the pull.

### Saying what is wrong before the download

Ollama's library is curated and Hugging Face's is not, so every row carries its
own warnings, read from the GGUF header Hugging Face returns inline. A chat
template with no tool support means chat but no Agentic. A missing template
means replies may arrive with raw markers in them. A gated repository needs an
account. A short window is called out. A vision model warns that its projector
is left behind by the import, so it lands text-only. All of it is a guess that
`/api/show` overwrites the moment the model is actually here.

Choosing a version is one dialog for both sources, because an Ollama tag and a
Hugging Face quantization are the same question. 25 rows with real sizes, the
native window, and which of them this machine can hold. The largest one that
fits leads the list with a star, since quantization trades accuracy for room and
nobody wants to be asked a question with twenty-five answers.

A card names the versions it already has rather than saying only that one is
installed, and stays installable, because a model you have is one you might want
another size of.

### The same model under two names

Nothing links an Ollama tag to a Hugging Face repository, so the same weights
can be downloaded twice from two places. Architecture and parameter count are
read from the same GGUF header by both libraries, so they identify the model
rather than the copy, and a row carrying an installed model's signature says so.
Where the architecture is not known yet, because an Ollama row is a family
rather than a file, the model's name stands in. That match is bounded so it has
to be the whole name and not the start of another: `qwen3.8` is the model in
`Qwen3.8-27B-GGUF`, but `qwen3` is neither that nor `qwen3-coder`.

The claim is deliberately weaker than "this is installed". An uncensored
fine-tune matches its base model on architecture and size while being a
different file, so the warning states what is known and leaves the judgement to
the reader.

### Capabilities come from Ollama

`/api/show` reports what a model can do, so there is no compatibility table to
maintain:

```
qwen3.8:27b      capabilities: ['completion', 'vision', 'tools', 'thinking']
qwen3-coder:30b  capabilities: ['completion', 'tools']
```

That matters. `think: true` on a model without a thinking mode is a hard 400
from Ollama, so the capability gates the request, and the client drops the key
and retries if a stale profile gets it wrong. Tool calling gates the agentic
role: a model without it cannot be assigned there, and the toggle is disabled
with the reason on it. Vision gates computer use.

`model_info` supplies the rest: the native context length, and the block and
head counts that size the KV cache.

### What fits

```
KV bytes per token = block_count * head_count_kv * (key_length + value_length)
```

At `q8_0` that is a byte per element. The formula is right for ordinary
attention and wrong for hybrids, so the catalog carries a measured figure for
models with real numbers behind them and the formula covers the rest. Profiles
say which they used. Qwen3.8 27B reads 8.1 GiB at 64k computed against 5.8 GB
measured, and the computed figure would refuse a 64k window on the card Aether
was built for.

### Effort levels

Which tiers think is a property of the tier. Whether they can is a property of
the model. The static hints describe the budget only, and each model appends its
own half: "Thinking on.", "Thinking off.", or "This model has no thinking mode."
The default agentic model reports no thinking capability, so without this the
slider promised a reasoning pass that never ran.

### Tool calls in the reply body

Weaker models write the call into the message instead of the `tool_calls` field.
Measured on llama3.2:3b with the real schemas, 2 of 16 replies did, both on the
deeply nested ones. A call is lifted out only when it names a registered tool
and its arguments are an object, and bare JSON is trusted only when it reaches
the end of the reply, so a call quoted mid-sentence is never executed.

When the JSON does not parse at all, which is the more common failure, nothing
is recoverable and guessing the arguments would run something the model never
asked for. The loop reports that a call was attempted and retries with a
correction, on a budget of its own, so a model alternating good and mangled
calls cannot retry forever.

## Delegation

`delegate` runs a sub-task on a fresh transcript, so its reading costs the
parent only the summary it hands back. Sub-agents cannot write files, so a
delegation can always be re-run.

Only offered on large projects. The schema costs 324 tokens on every request,
and on a small project there is no context pressure for a fresh window to
relieve.

## Computer use

One tool, `computer`, with a screenshot and the desktop actions behind it. Every
call returns the screen its action produced, and the model reads that image
before choosing the next one.

That loop is the whole feature. The first version had no tool at all, just a
prompt line saying screenshots "may be attached" plus the ordinary shell. The
model built its own driver out of that, downloaded xdotool into `/tmp`, typed a
search into the browser, and then had no way to look at the result, so it typed
the search again. Acting without observing is not computer use.

Input goes through XTest on X11 and SendInput on Windows, both over `ctypes`.
Neither needs a package Aether does not already ship, which matters because the
version that required a binary the user had to install first is the one that
failed.

The model is shown a frame whose long edge is capped at 2000px and sends
coordinates in that frame's pixels, and the tool scales them onto the display.
Text too small to read is a second screenshot with a `region`, cropped at native
resolution. Any frame scaled under 60% says in its own result that body text
will not be readable in it.

Only the last two frames keep their pixels, in the prompt and in storage. A
frame is ~1k tokens and ~40KB of base64, the transcript is rewritten after every
tool call, and only the newest one shows the screen as it is now.

Two guards replace the step ceiling the loop deliberately does not have. Frames
are compared on a 48x27 grid of cell brightnesses, so a button that changed
shade counts as a response and a blinking cursor does not. That answer folds
into the duplicate-call signature, which makes clicking the same button twice
ordinary work and clicking it twice with nothing happening a repeat. And four
actions in a row that leave the screen untouched end the run with a debrief,
because a model getting nowhere does not repeat itself exactly, it nudges the
coordinates, which is a new signature every time.

### Editing guards

Three refusals sit in `guards.py`, shared by the desktop and browser tools. They
all come from one run that wrote a finished two-page paper into Google Docs and
then destroyed it.

`ctrl+a` selects the whole document, and the model used it meaning "select the
line I just typed", then pressed Return. That replaced the paper with a blank
line, three times. So a select-all is remembered, and typing or a replacing key
while it stands is refused unless the call repeats with
`confirm: "replace_selection"`. The capability is kept and the intent has to be
stated. Formatting keys pass through, because bolding a selection keeps it.

Only the keyboard clears the flag. Clicking used to, until a second run pressed
ctrl+a, clicked six times at the font-size box trying to enlarge its title, and
pressed Return. A toolbar click leaves the document selected, so the guard had
stood down right before the keypress that mattered.

That run also clicked where it thought the body should start, landed inside the
title, and typed 664 characters into the middle of it, which is why the whole
thing came out bold and 24pt. So the prompt carries a document recipe: work top
to bottom, never click inside text you have written, and set the paragraph style
from the keyboard before typing the line rather than hunting toolbar buttons
that are a few pixels wide in a scaled frame.

Typing reports what it sent, not what arrived. "Typed 664 characters" was a
claim the tool cannot support, since one ctrl+z later the page held 498 of them,
and the model built on it.

Fifty-five Returns went into the same run, in bursts, trying to reach a place on
the page. Past four with nothing typed in between they are refused. Blank lines
are not layout.

The rules and the state both live in `guards.py` rather than in either tool,
because the browser arrived later with the same reach into the same document and
none of the guards, and a run sent ctrl+a through that door fourteen times
before anything stopped it. A selection made with one tool is still a selection
when the other one types.

### Turns, not milliseconds

A run was measured at 15.5s a step and 0.7s of that was the action. What a
desktop job costs is how many times the model has to look and decide. So a call
carries `actions`, a list run in order with one screenshot at the end, and a
heading that was six turns is one. The batch stops at the first refusal and
reports which steps ran, because applying the rest of a sequence whose middle
did not happen leaves the model reading a screen it cannot account for.

### Aiming

Three ways to say where, in order of how much is left to chance.

`browser` is the first. Inside a web page nothing needs aiming at all.
`snapshot` returns elements with refs and names, `click` and `fill` take a ref,
`read` returns text rather than pixels, and typing is one insert instead of 664
keystrokes. A click is refused when something covers the target, so a run cannot
dismiss a cookie banner and report that it pressed Bold. "Covered" has to mean
it: sampling only an element's centre called thirteen Docs menu items covered by
their own wrappers, so the locator tries several points across the element.

Aether drives its own Chrome profile in data/browser. Chrome will not share a
profile between two instances, and taking over the window someone is reading is
not a thing to do by surprise. That profile is signed in to nothing until
someone signs it in. A page asking for a password is reported as a thing to stop
on rather than a wall to get past, `fill` refuses a password field whatever it
is asked, and Aether attaches instead of launching when a browser is already
listening on the port, which is how you give a run a real logged-in session on
purpose.

`elements` is the second, for native windows. The accessibility bus gives real
widgets with names and boxes, and a click on a ref lands in the middle of one.
Boxes that fall outside their own window are dropped, because toolkits do lie. A
window manager reports its title-bar buttons at (0, 0, 22, 22) whatever the
window's position, and a ref aiming at the corner of the display is worse than
no ref.

Coordinates are the fallback, for what exposes neither. That is the path that
produced six clicks at the same wrong spot on a button fourteen frame-pixels
wide.

### Looking is never repeating, but it is not working either

Both halves were learned the hard way, in that order.

The duplicate-call guard counts identical arguments and infers an identical
result. That holds for reading a file and is false for an observation, since the
point of a second snapshot is that a menu opened since the first. A run in
Google Docs had six of its snapshots refused as duplicates, which took away the
one move that gets a stuck run unstuck, and it spent its last twenty-six calls
unable to see. Observations are exempt now.

Exempting them was right and it was also the only brake there was. The next run
took 265 screenshots in a row without acting on one of them, for 78 minutes, and
never wrote a word of its debrief. So looks are counted separately, and past
five the run is told that looking again will show it the same screen. Anything
it actually does resets that to zero, so a run alternating look and act runs as
long as it likes.

The same guard asks each tool whether the world moved, and each answers for
itself: the screenshot's frame id for the desktop, a counter the browser bumps
on anything that changes a page. A run that only used the browser had no answer
at all, so the world looked frozen and twenty-one legitimate repeat clicks came
back refused.

### The model

Grounding is a separate skill from vision. qwen3.8 can read a toolbar and still
not say where its Bold button is. When nothing has been chosen, computer use
prefers a model trained to point, the qwen3-vl family, over a general one, and a
model the user picked still wins. qwen2.5vl is deliberately not on that list,
because Ollama reports it without tool calling, so it can see a screen and never
act on one.

The desktop is marked while this runs: a border in the brand colours and a pill
naming the current action, both click-through, raised when a run starts rather
than when the switch is flipped. GTK draws it, with Tk as a fallback. The
original was Tk-only and punched its transparent hole with `-transparentcolor`,
which is Windows-only, and it never got that far anyway because Aether's venv
has no `_tkinter`. Both failures were invisible, since `set_overlay` caught the
import error and reported success.

## Connections

Which accounts Aether may act in, in one place, in two kinds.

A *session* connection is a sign-in the user performs themselves, once, in
Aether's browser window. Aether stores nothing. What persists is the cookie in
that browser profile. This is not a shortcut around building a password box, it
is the only thing that works, because Google, Microsoft and most of the rest
refuse scripted sign-in from an instrumented browser. It is also the safer
shape, since a stolen profile costs one revocable session rather than a
password. Whether it took is judged by where the browser lands when it asks for
a page only a signed-in user reaches, because every service redirects you to its
login page and none of them agree on what that page looks like.

A sign-in never starts on the service's page. It starts on Aether's own, served
at `/connect/{id}`, which says who is asking and for what before handing anyone
to a login form. A browser window the user did not open, landing straight on
somebody's sign-in, tells them nothing about who opened it. In use it was worse:
the Google sign-in succeeded, then Chrome's own account machinery carried the
window off to a third party's OAuth screen, and nothing on screen had ever said
Aether. So that window is launched with Chrome's sign-in and sync switched off,
and Aether pins the tab it drives rather than taking whichever page comes back
first.

A *secret* connection is an API key, for services that authenticate that way. It
goes to the operating system's keyring and nowhere else. A machine without one
is told so rather than having a token written to a file beside the code and
called stored. The model is told which connections exist, by name, because that
changes its plan and stops a run trying to get past a login it cannot pass. It
is never shown a value.

The registry is deliberately general: id, kind, how to establish it, how to
check it. That is the same shape a mail account, a calendar or an MCP server
needs.

## File and shell safety

Reads and writes are limited to registered projects, Aether's folder, Documents,
Desktop, and uploads.

`edit_file` matches surrounding text instead of line numbers. Ambiguous anchors
are refused, and a failed one gets the closest real lines back so it can be
fixed in one attempt.

`write_file` will not create over an existing file without `overwrite`, and will
not replace a substantial file with much less content, which is almost always a
regeneration dropping whatever the model did not retype. Every write snapshots
to `data/backups/` first.

Writes are syntax-checked before they commit, including inline `<script>` in
HTML with line numbers mapped back to the host file. The check fails closed.

Shell commands run against a denylist, not an allowlist. An allowlist blocks
ordinary work like `cp`, `python` and `docker` far more often than it blocks
anything dangerous. `safe_auto` stops four things: destroying data, escalating
privilege, changing the machine, and running downloaded code. Commands are lexed
with `shlex` and matched on program name, including through `find -exec` and
`xargs`.

## When the agent needs you

Approvals and clarifying questions share one bar above the composer. Several
stack and page through, and a multi-part question gets tabs.

The server holds the open question keyed by chat id, and the UI polls
`/api/chats/{id}/pending` while a run streams. That polling is the recovery
path. Without it, a question whose event never reached the browser leaves the
run stalled until it times out with nothing on screen. Sub-agent questions are
filed under the parent chat's id.
