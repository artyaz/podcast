# Praxis

Turns a question into a researched, podcast-ready lesson you can edit, interrogate, and play back block by block.

The backend is a LangGraph research loop that is required to use its tools, audit its own gaps, and mark every claim's evidential status. The frontend is a block editor where you can put the cursor anywhere, type or speak a question, and get an answer inserted directly beneath it — in the voice of the surrounding episode, because the whole thing is written to be spoken.

---

## What was verified against the live APIs

Everything below was established by calling the real endpoints, not by reading documentation. Several documented-looking assumptions turned out to be wrong, so this list is the contract.

| Capability | Endpoint | Notes |
| --- | --- | --- |
| Chat | `POST {base_url}/chat/completions` | `~deepseek/deepseek-v4-flash-latest` is a real alias; it resolves to `deepseek/deepseek-v4-flash-0731`. Tool calling works, one call per turn. |
| Model list | `GET {base_url}/models` | Carries the reasoning descriptor the settings screen is built from. |
| Transcription | `POST https://openrouter.ai/api/v1/audio/transcriptions` | Multipart. `openai/whisper-large-v3-turbo` **rejects** `/chat/completions` with an HTTP 400 telling you to come here. |
| Speech (Kokoro) | `POST https://openrouter.ai/api/v1/audio/speech` | Takes `input`, not `messages`. Returns raw MP3 bytes. |
| Speech (Speechify) | `POST https://api.sws.speechify.com/v1/audio/speech` | Returns base64 in a JSON envelope, plus `speech_marks` timings and `billable_characters_count`. |
| Search | `POST https://api.exa.ai/search` | See the mode table below. |
| Page read | `POST https://api.firecrawl.dev/v2/scrape` | Real remaining credits at `/v2/team/credit-usage`. |

Two things that do **not** exist, despite looking like they should:

- **Exa's Research API is retired.** `/research/v1`, `/research/v0` and `/research` all answer HTTP 410. Synthesis across a literature is the agent's own work, done with repeated searches at increasing depth.
- **Kokoro has no voice listing.** `/api/v1/audio/voices` is a 404, and an unrecognised voice ID fails the whole call. The seven voices in the picker were each confirmed individually; the prefix is the accent (`af`/`am` American, `bf`/`bm` British).

### Exa search modes, measured

The full enum is `neural | keyword | auto | hybrid | fast | blue | deep-reasoning | deep-lite | magic | deep | instant`. Cost and latency for a two-result query:

| Modes | Cost | Latency |
| --- | --- | --- |
| `magic`, `neural`, `instant` | $0.007 | 0.1–0.3s |
| `fast`, `hybrid`, `blue` | $0.007 | 0.6s |
| `auto` | $0.007 | 1.2s |
| `deep`, `deep-lite` | $0.012 | 3.0–4.5s |
| `deep-reasoning` | $0.015 | 5.7s |

The model picks the mode per query and is told these numbers, so it can spend deliberately: cheap modes to confirm a date, `deep-reasoning` when sources are known to disagree.

### Reasoning, and why the control is built from model metadata

Each model's entry in the catalogue carries a `reasoning` descriptor, and it is
the only honest basis for a reasoning setting. Guessing gets punished:

| Model | Descriptor | Consequence |
| --- | --- | --- |
| `deepseek/deepseek-v4-flash` | `mandatory: false, default_enabled: true, supported_efforts: [max, high, low]` | Reasoning is **on** unless explicitly disabled, and there is no `medium` |
| `deepseek/deepseek-r1` | `mandatory: true` | `{"enabled": false}` returns **HTTP 400 "Reasoning is mandatory"** |
| `openai/gpt-5` | `mandatory: true, supported_efforts: [high, medium, low, minimal]` | Cannot be turned off; four effort levels |
| `deepseek/deepseek-chat` | `null` | No reasoning at all; the parameter is ignored |

So the settings screen hides the control entirely for a model with no reasoning,
hides the Off switch for a model that requires it, and populates the effort list
from that model's own `supported_efforts` rather than a fixed low/medium/high.
Switching models re-validates the saved choice, because carrying `medium` from
gpt-5 over to deepseek-v4-flash would send a value that model never advertised —
which is not rejected, just silently reinterpreted.

Two measured details shape the rest. `exclude: true` stops the reasoning text
coming back but **not** the thinking: a call with `effort: low, exclude: true`
returned zero trace characters and still billed 13 reasoning tokens. And
reasoning tokens are spent from the same `max_tokens` pool as the answer, which
truncated a JSON scoping response at 1460 characters that completed cleanly with
reasoning off — so the profile adds a reasoning allowance on top of the requested
ceiling.

Because the endpoint is configurable, all four paths — chat, transcription,
speech, and the model list — are read from one base URL. OpenRouter is the
default; any OpenAI-compatible server works, and one that implements only chat
still works with a degraded model picker.

### Breaking a subject into segments

`POST /api/outline` divides a subject into an even spine of segments before any
research happens: one cheap call, no tools, and the result is the listener's to
accept, prune, or regenerate. Accepting it changes the run in two places —
scoping is told the segments are fixed and must distribute its open questions
across them, and the writing step is told to cover them in order with comparable
weight. The spine travels in the checkpoint, so it survives every resume.

It is worth using when a subject is broad enough that a single scoping pass would
skim it. A narrow question does not need it.

### Structured output is validated, not trusted

This model returns syntactically perfect JSON with the right keys and nothing in
them. A scoping call was observed returning an empty governing axis and an empty
question list; the graph accepted it and carried the emptiness forward as though
the question had been scoped. So every structured call now declares which keys
must be present and which must be non-empty, and a response failing either is
retried rather than returned. Truncated responses are never salvaged by slicing
between braces — a partial object that happens to parse is worse than a parse
error, because the caller believes it.

---

## Architecture

```
/                      SvelteKit 5 frontend (prerendered, no SSR)
  src/lib/settings.svelte.ts   keys, rotation counters, voices — localStorage only
  src/lib/lesson.svelte.ts     blocks, the resume loop, inline asks
  src/lib/audio.svelte.ts      per-block synthesis, cache, sequential playback
  src/lib/components/          BlockView, BlockActions, AskBar, SubtopicModal
/api                   FastAPI + LangGraph on the Vercel Python runtime
  praxis/keys.py         multi-key pools, quota-aware ordering
  praxis/transport.py    HTTP plus the rotation retry loop
  praxis/tools.py        exa_search (mode routing), firecrawl_scrape
  praxis/prompts.py      the Praxis persona, retargeted for speech
  praxis/graph.py        the resumable research graph
  index.py               SSE endpoints
```

### The 300-second problem, and why the graph resumes

Vercel's Hobby plan terminates any invocation at 300 seconds. A deep research run on a contested question does not reliably fit in 300 seconds.

So every node checks the clock before starting. If there is not enough time left to finish the work it is about to begin, it suspends without starting, and the state travels back to the browser, which immediately posts it to the same endpoint again. The graph re-enters at the phase it stopped on. From the reader's side it is one long operation with a progress rail.

That constraint forces a discipline worth having anyway. Anything crossing an invocation boundary has to fit in a request body, capped at 4.5 MB, so **raw scraped pages cannot travel.** Pages are read, distilled into claims with sources and a status, and dropped inside the invocation that fetched them. The checkpoint carries conclusions, never corpora — a real run checkpoints at a few kilobytes.

One subtlety, found by testing rather than reasoning: the naive rule "start only if the estimated cost fits in the time remaining" **livelocks** when the whole budget is smaller than one node's estimate. The graph suspends, the browser posts the identical state back, and it suspends again at the same phase forever, burning an invocation each time. So a node also starts if nothing has run yet in this invocation: being cut off mid-node loses that node's work, but never starting loses everything.

### Phase order

```
scope → research → distill → gap_check → research (again) or write → done
```

`gap_check` is the loop's conscience. It audits what is still missing and can send the graph back for another round — and it is **not permitted** to declare readiness before the configured minimum number of rounds has actually happened, or while fewer than four findings are verified or contested. "Always do deep research" has to be structural, or a confident model declares itself done after one cheap search.

### Key rotation

The backend is stateless, so the browser owns the key list and the usage counters and sends both with every request; the response hands back updated counters. Ordering puts keys with known remaining credits first (descending), then least-spent-first. A key is retired for the rest of a request on 401, 402, 403 or 429 — and only those, because a 500 or a timeout is a problem with the request, not the key, and rotating would burn every key on the same broken call.

Firecrawl reports true remaining credits, so rotation reads them. Exa has no balance endpoint, so its reported per-call cost is accumulated instead.

---

## Deploying

The frontend is prerendered and talks to the backend over `fetch`, so either topology works with no code change.

### One project (default)

`vercel.json` maps every `/api/*` request into the ASGI app at `api/index.py`, which routes internally. Leave **Backend URL** empty in Settings.

Two things about this layout are load-bearing, and both were learned by breaking them.

**`requirements.txt` stays in `api/`, beside the function — not at the project root.** Moving it to the root looks tidier and breaks the build outright:

```
Error: The pattern "api/index.py" defined in `functions` doesn't match
any Serverless Functions inside the `api` directory.
```

A root `requirements.txt` naming FastAPI is exactly what Vercel's Python framework detection matches on, and a Python framework preset takes precedence over file-based functions — so `api/index.py` stops being a function and the pattern matches nothing. Pinning `framework: "sveltekit"` does not rescue it. Keeping the requirements file next to the function keeps this a SvelteKit project that happens to contain one Python function, which is what it is.

**`api/index.py` puts its own directory on `sys.path`** before importing `praxis`, because functions run with the project root as the working directory, so a bare `import praxis` is not guaranteed to resolve.

If the function fails to import anyway, `/api/health` answers with the traceback, the Python version, the working directory, `sys.path`, and the list of packages that actually got installed — rather than the blank 500 that Vercel returns for an import-time crash. That diagnostic is the entire reason the entrypoint is a thin loader: from outside a deployment, a missing dependency, a wrong runtime version and a real bug are otherwise indistinguishable.

Python functions get a 500 MB uncompressed bundle (Node gets 250 MB), which LangGraph fits inside comfortably. Billing is on active CPU, and a research loop is almost entirely I/O wait, so waiting on Exa and the model is not billed.

### Two projects

Deploy `api/` as its own Vercel project, then put its URL into **Backend URL** in Settings. Use this if the single-project build gives you trouble, or if you want to scale them separately.

### Vercel Services

Vercel also supports one project with a SvelteKit service at `/` and a Python service at `/api` sharing a domain. It is the tidiest option, but the feature is permissions-gated, so the default config above deliberately does not depend on it.

---

## Running locally

```bash
npm install
npm run dev                     # frontend on :5173, proxies /api

python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd api && ../.venv/bin/python -m uvicorn index:app --port 8000
```

Set `PRAXIS_BACKEND_URL` if the backend is not on `http://127.0.0.1:8000`.

---

## Keys

None are shipped. Every key is entered in Settings, stored in this browser's `localStorage`, and sent with each request. Add several per provider to get rotation.

- **OpenRouter** (or any OpenAI-compatible endpoint) — required. Model, transcription, and Kokoro speech.
- **Exa** — required. Search.
- **Firecrawl** — optional but strongly recommended. Without it the agent can only read search snippets, never a full statute or paper, and snippets strip exactly the qualifications the argument lives in.
- **Speechify** — optional. Better voices and word-level timings.
