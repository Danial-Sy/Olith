# Local performance check

Measured on October 15, 2026, using 85 text files and 2,358 chunks from this project.
Hardware was an i9-12900K with about 32 GB RAM, running Ubuntu and Python 3.12.3. Nomic
embeddings used CPU. This checks code search. It does not measure how much faster full
coding tasks finish.

## Results

| Work | Measured time |
|---|---:|
| Build an empty syntax/text index | 0.83 s |
| Refresh the unchanged index | 19 ms, zero files reparsed |
| Old v1.5.0 repeated Python outline | 20.9 ms |
| Cached repeated Python outline | 0.012 ms |
| Old regex search, given the exact definition name | 7.1 ms |
| Indexed symbol search, given the name | 31.8 ms |
| Text search, given a natural-language question | 26.8 ms |
| Dependency map, capped at 1,400 estimated tokens | 57.4 ms |
| First complete CPU embedding build | 331.7 s (about 5½ minutes) |
| Hybrid search with CPU embeddings | 382 ms |

The table uses median times. Search results use the median of eight question medians.
Source-only checks ran seven times; semantic checks ran five. Each full index build ran
once. Filesystem caches were warm. Maps contained 1,353–1,396 estimated tokens. The full
semantic cache used about 16.6 MiB.

For eight hand-picked questions with expected function names fixed beforehand:

- Text-only natural-language search returned the expected function in 5/8 cases.
- Hybrid semantic search returned it in 7/8 cases.
- Exact-name regex and symbol search returned it in 8/8 cases, with the correct
  name supplied by the benchmark rather than discovered by a model.

A match counts if the expected function appears in the returned results, up to eight
hits. The snippet limit can return fewer. Eight hand-picked questions are too few to
claim an accuracy score for other projects. The missing semantic target was `save_chat`;
semantic search is useful, but it isn't guaranteed to find the answer.

## What that means

Cached parsing saves repeated work, and semantic search helps with questions where you
don't already know the identifier. Exact-name regex search is still faster here, so
keeping it as the default makes sense. Semantic search adds about 0.38 seconds per
lookup on this setup; maps add about 0.06 seconds before a run. Tests, builds and
language-server startup add their own time. These changes do not make the model generate
tokens faster.

In a separate controlled check, Qwen3.8 27B stayed loaded across a CPU embedding
request, with the same reported GPU memory before and after. That check used a
2,048-token chat context. Larger contexts or other Ollama setups may behave differently.

These measurements do not compare complete agent tasks before and after the change. They
cannot support a claim like "daily use is X% faster." Better context and fewer model
calls still need a controlled task comparison.

## Run it yourself

```bash
.venv/bin/python scripts/bench_repo_index.py --baseline-ref v1.5.0 --repeats 7
.venv/bin/python scripts/bench_repo_index.py --semantic --repeats 5
```

`--cases` accepts a JSON list containing `query`, `regex`, `file`, and `symbol` for each
question. Keep it outside the project or under ignored `data/` so the benchmark's
answers don't enter the code index. `--output` saves a full JSON report.
`--baseline-ref` executes the read-only tools from a trusted local Git ref against a
temporary copy of the same eligible source files. No project source is changed. Semantic
measurements require the model to be installed already.

The first run let old regex search ignored runtime files too. That made its timing look
worse, so those results are left out. The table compares the same source files.
Embedding times come from the separate semantic run.
