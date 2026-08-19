# Praxis

Turns a question into a researched, podcast-ready lesson you can edit, interrogate, and play back block by block.

The backend is a LangGraph research loop that is required to use its tools, audit its own gaps, and mark every claim's evidential status. The frontend is a block editor where you can put the cursor anywhere, type or speak a question, and get an answer inserted directly beneath it — in the voice of the surrounding episode, because the whole thing is written to be spoken.

---

## What was verified against the live APIs

Everything below was established by calling the real endpoints, not by reading documentation. Several documented-looking assumptions turned out to be wrong, so this list is the contract.

| Capability | Endpoint | Notes |
| --- | --- | --- |
| Chat | `POST https://openrouter.ai/api/v1/chat/completions` | `~deepseek/deepseek-v4-flash-latest` is a real alias; it resolves to `deepseek/deepseek-v4-flash-0731`. Tool calling works, one call per turn. Reasoning is explicitly disabled. |
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

---

## Architecture

```
/                      SvelteKit 5 frontend (prerendered, no SSR)
  src/lib/settings.svelte.ts   keys, rotation counters, voices — localStorage only
  src/lib/lesson.svelte.ts     blocks, the resume loop, inline asks
  src/lib/audio.svelte.ts      per-block synthesis, cache, sequential playback
  src/lib/components/          BlockView, BlockActions, AskBar
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

`vercel.json` maps every `/api/*` request into the ASGI app at `api/index.py`, which routes internally. Deploy the repo as a single Vercel project with the SvelteKit preset; the Python function is built from `api/requirements.txt`. Leave **Backend URL** empty in Settings.

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

cd api
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn index:app --port 8000
```

Set `PRAXIS_BACKEND_URL` if the backend is not on `http://127.0.0.1:8000`.

---

## Keys

None are shipped. Every key is entered in Settings, stored in this browser's `localStorage`, and sent with each request. Add several per provider to get rotation.

- **OpenRouter** — required. Model, transcription, and Kokoro speech.
- **Exa** — required. Search.
- **Firecrawl** — optional but strongly recommended. Without it the agent can only read search snippets, never a full statute or paper, and snippets strip exactly the qualifications the argument lives in.
- **Speechify** — optional. Better voices and word-level timings.
