# Pollinations.ai — full capability research (2026-10-07)

Research doc, not a build plan — same "save findings, don't auto-schedule
a build" pattern as `GUILD_CURRICULUM_RESEARCH.md`. Captures everything
Coffee pasted from Pollinations' docs (dev-bridge, 2026-10-07) plus what's
already confirmed live in this codebase, so a future feature decision has
real facts to work from instead of re-researching this from scratch.

## Two completely different Pollinations products — don't conflate them

This game **already uses** Pollinations, but only its original, narrow,
free image service:

- **`image.pollinations.ai`** (`images.py`) — a plain HTTP GET, **free,
  no API key, no account, no cost**. One capability only: text-prompt →
  image. This is what powers every location image, NPC portrait, item
  art, Labyrinth room tile, battle formation background, etc. across the
  whole game today. `POLLINATIONS_BASE = "https://image.pollinations.ai/prompt/"`.

Everything Coffee pasted on 2026-10-07 describes a **different, newer,
separate product**:

- **`gen.pollinations.ai`** (+ `enter.pollinations.ai` for auth,
  `media.pollinations.ai` for file storage) — a full OpenAI-compatible
  multi-modal gateway: text, image, video, audio/TTS/STT, realtime voice,
  3D, embeddings, agents. **Every single generation request requires a
  real API key and spends real currency ("Pollen")** — this is NOT an
  extension of the free image GET endpoint this game already relies on.
  Free image generation through `gen.pollinations.ai`'s own `/image/`
  route is NOT confirmed free the same way `image.pollinations.ai` is;
  the docs are explicit that "All generation requests require an API key."

**Practical takeaway**: nothing here is a drop-in free upgrade to what
already works. Adopting ANY capability below means creating a real
account at enter.pollinations.ai, getting a `sk_...` secret key, storing
it in `.env` (same pattern as `GITHUB_TOKEN`), and accepting that usage
now costs real money/Pollen (though a free "Quest Pollen" allowance
exists per-account — amount/renewal not detailed in what was pasted).

## What `gen.pollinations.ai` actually offers

**Text generation** — OpenAI-compatible Chat Completions (`/v1/chat/completions`),
a stateless Responses API (`/v1/responses`), and an Anthropic Messages-shaped
endpoint (`/v1/messages`, works with Claude Code/Anthropic SDKs by pointing
`ANTHROPIC_BASE_URL` at it). Huge model catalog — GPT-5.x/6.x, Claude
(including `anthropic/claude-sonnet-5`, `opus-5`, `fable-5.1` — the exact
family this session itself runs on), Gemini 3.x, DeepSeek, Grok, Llama 4,
Mistral, Qwen3, and many more, selected by a `publisher/model` string.
Supports streaming, tool calling, reasoning effort levels, vision input,
structured outputs, prompt caching (Gemini/Claude/Nova).

- **Relevance to this game**: this project's own narration pipeline
  (`ai/dm_agent.py` etc.) already runs against a local Ollama instance
  (`lfm2.5-thinking`), deliberately free and local — CLAUDE.md is explicit
  that CPU-only local inference is the current architecture, with real
  documented latency tradeoffs (30-160s/call) already designed around.
  Routing narration through Pollinations' hosted models instead would
  trade "free, slow, local" for "paid, probably much faster, cloud" —
  a real architectural decision, not a quick swap, and introduces a
  recurring cost that doesn't exist today.

**Image generation** — `GET /image/{prompt}` or `POST /v1/images/generations`,
many more models than the current free endpoint offers (Flux variants,
Gemini image models, Seedream, Ideogram, GPT-image, etc.), plus
`/v1/images/edits` (image-to-image editing — not available on the
currently-used free endpoint at all).

- **Relevance**: the free `image.pollinations.ai` endpoint already covers
  this game's actual current need (one-shot text→image, no editing). The
  one genuinely new capability is **image editing** (e.g. "take this
  existing character portrait and change its pose/gear") — not something
  this game does today, would be new scope, not a fix to anything broken.

**Video generation** — `GET /video/{prompt}` or the OpenAI-shaped
endpoint, several models (Veo, Seedance, Wan, Grok Imagine), returns MP4,
synchronous (waits for completion, up to the provider's own timeout).

- **Relevance**: no video capability exists anywhere in this game today.
  Could theoretically generate a short cinematic clip for a chapter
  climax (this game already has `CUTSCENE_VIDEO_PROMPTS.md` — written
  prompts for cutscene video that were never actually generated/shipped,
  per the Chapter 7 memory note). This is the most directly relevant new
  capability if Coffee ever wants to revisit that backlog item — but
  real cost per video generation, real latency, and Telegram's own file
  size/type handling would all need scoping first.

**Audio (TTS/STT/music)** — `GET /audio/{text}` or OpenAI-shaped
`/v1/audio/speech` (TTS) and `/v1/audio/transcriptions` (STT), big voice
catalog (ElevenLabs, OpenAI, Google, etc.), plus dedicated music-generation
models (`google/lyria-3.5`, `elevenlabs/music-v2`).

- **Relevance**: this game **already has two working, free TTS backends**
  (`ai/tts_piper.py`, local/free; `@TextTSBot`, a free third-party
  Telegram bot — see `db.get_setting("tts_backend")`, bot.py ~5366) and a
  working STT path (`ai/stt_groq.py`, free-tier Groq). Pollinations' audio
  offering would be a third, PAID alternative to something that already
  works for free — real justification needed (e.g. a specific voice
  quality or music-generation need the current free backends can't do)
  before this is worth the cost.

**3D generation** — Trellis/Asset Harvester/Rodin, returns GLB or PLY
Gaussian-splat files.

- **Relevance**: none identified. This is a text-based Telegram game with
  no 3D rendering surface anywhere — flagged for completeness only.

**Realtime voice (WebSocket)** — OpenAI Realtime-protocol voice sessions.

- **Relevance**: Telegram bots don't have a native live-voice-call
  surface this would plug into; would require building an entirely new
  interaction mode. Out of scope unless Coffee specifically wants a
  voice-chat feature.

**Embeddings** — `/v1/embeddings`, OpenAI-compatible, several models
(Gemini, OpenAI, Cohere, Qwen), supports text/image/audio/video input
depending on model.

- **Relevance**: this is the one capability that doesn't compete with
  anything already built. Real potential uses: smarter NPC memory
  recall (semantic similarity instead of exact keyword match), better
  Support-agent grounding (semantic search over the item/spell/guild
  catalogs instead of rebuilding the whole catalog into every prompt),
  or Moltbook feed relevance scoring. Genuinely new capability, worth a
  real look if a concrete need comes up — not scoped here.

**Agents / MCP / model publishing** — lets you register a prompt-based
or code-based "agent" as a callable model, with configured MCP tool
access (including a `computer` sandbox, web search, etc.), publishable
to Pollinations' own catalog.

- **Relevance**: this is Pollinations' own agent-hosting platform, aimed
  at developers building things ON Pollinations — not something this
  project would consume, since this game already has its own real agent
  logic (`ai/dm_agent.py`, `ai/npc_agent.py`, `ai/support_agent.py`,
  `ai/autonomous_player.py`) running against local Ollama. Flagged for
  completeness only.

**Connect User Wallets (BYOP)** — lets an app's own end-users pay for
their own Pollinations usage via OAuth, instead of the app owner footing
the bill.

- **Relevance**: none — this game has one owner-funded bot, not a
  multi-tenant app where individual players would authorize their own
  spend. Flagged for completeness only.

## Pricing / access model (as documented, not independently verified)

- Currency is "Pollen." Three key types: `sk_` (secret, server-side,
  full access), `pk_` (publishable App Key, for OAuth/BYOP flows), legacy
  `pk_` raw publishable (rate-limited to 1 pollen/IP/hour, "do not mint
  new ones").
- A "Quest Pollen" free allowance exists per account (exact amount/refresh
  not stated in what was pasted) separate from paid Pollen.
- Token prices capped at 50 Pollen/1M tokens; fixed image price capped at
  0.25 Pollen/image; video capped at 0.5 Pollen/generated second;
  transcription capped at 0.012 Pollen/minute (these are PUBLISHER-side
  caps for people publishing their OWN models to the catalog, not
  necessarily what using an existing official model actually costs per
  call — the docs pasted don't give a clear per-call price table for
  consuming official models).

## Live-verified correction (2026-10-07, same day)

Coffee said he doesn't want to pay for anything. Before assuming the
legacy free endpoints (`text.pollinations.ai`, and its `?model=
openai-audio` TTS variant) were still usable, they were tested directly
with real `curl` calls rather than trusted from secondhand articles:

- `image.pollinations.ai` — **confirmed still free/keyless and working**
  (HTTP 200, real image bytes returned). This is the one genuinely free
  capability, and it's exactly what this game already uses.
- `text.pollinations.ai/{prompt}` — **returns `402 Payment Required`**
  on every model tried (`openai`, `mistral`, no model specified).
  Despite the endpoint's own deprecation notice claiming "Anonymous
  requests to text.pollinations.ai are NOT affected," that is not what
  actually happens today.
- `text.pollinations.ai/{prompt}?model=openai-audio` (free TTS) —
  **returns `404 Model not found`**; that model no longer exists on the
  legacy route at all.

So in practice, as of today, **nothing beyond plain image generation is
free**. Shipped the one real, zero-cost improvement available: pinned
the free image endpoint to an explicit `model=flux` (`images.py`,
v1.27.742) instead of whatever Pollinations' own default model happened
to be — same free endpoint, no new dependency, consistently better
output. Everything else in this document (video, embeddings, hosted
text, audio, 3D, realtime voice, agents) remains genuinely paid-only;
there is no free path to any of it right now.

## Bottom line

Nothing here is urgent or broken — this game's existing free image
pipeline, free local narration, and two free TTS backends all keep
working exactly as they do today, completely independent of any of this.
The two capabilities worth a real look if a concrete need comes up later:
**video generation** (ties into the already-written but never-shipped
`CUTSCENE_VIDEO_PROMPTS.md` backlog) and **embeddings** (smarter
NPC/Support-agent recall). Both would need their own scoped plan,
including getting a real funded API key, before any code gets written.
