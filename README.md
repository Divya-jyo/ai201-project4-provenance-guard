# Provenance Guard

A backend that classifies submitted creative text as likely AI-generated,
likely human-written, or uncertain -- combining two independent detection
signals into a calibrated confidence score, a plain-language transparency
label, and an appeals workflow for contested classifications.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add your GROQ_API_KEY
python app.py
```

Server runs at `http://localhost:5000`.

## Architecture overview

A submission hits `POST /submit` and passes through both detection signals
independently: a stylometric check (pure Python, structural) and an LLM
check (Groq, semantic). Their outputs are combined into one score, that
score is classified against asymmetric thresholds, turned into label text,
and the whole thing -- text, both raw signal scores, combined score,
attribution, label -- is written to a structured SQLite audit log before
the response is returned.

An appeal (`POST /appeal`) doesn't touch detection at all: it looks up the
original submission by `content_id`, flips its status to `under_review`,
and attaches the creator's reasoning to that same audit-log row, leaving
the original decision visible for a human reviewer.

```
POST /submit --> stylometric_signal ---+
             \--> llm_signal ----------+--> combine_scores --> classify --> generate_label --> audit log --> response

POST /appeal --> lookup by content_id --> status=under_review + reasoning logged --> response
```

Full diagram with intermediate value labels lives in `planning.md` under `## Architecture`.

## Detection signals

**LLM signal (Groq, `llama-3.3-70b-versatile`).** Asks the model to judge
holistically whether text reads as human or AI-generated. Chosen because
it captures semantic/contextual cues -- tone, coherence, the specific kind
of "genericness" AI text tends toward -- that no formula easily captures.
**What it misses:** it's a black box. We can't inspect why it scored
something the way it did, and it can be confidently wrong in ways that are
hard to predict.

**Stylometric heuristics (pure Python).** Computes sentence-length
variance and type-token ratio (vocabulary diversity), on the premise that
AI text tends toward more uniform sentence length and word choice while
human writing is more irregular. **What it misses:** needs enough text to
be meaningful. We tested it directly on four ~3-4 sentence reference
samples (one clearly AI, one clearly human, two borderline) and it
produced scores within 0.06 of each other across all four -- at that
length, sentence-count and vocabulary don't vary enough to carry signal.
It becomes more useful on longer submissions (a full short story excerpt,
a blog post), which is the use case this project targets.

These two signals are independent in a meaningful way: one is a holistic
semantic judgment, the other is a measurable structural property. They can
and do disagree, which is the point -- agreement raises confidence,
disagreement should (and does, via the threshold design) push toward
"uncertain" rather than a forced pick.

## Confidence scoring

`combined_score = 0.75 * llm_score + 0.25 * stylometric_score`

LLM weighted higher because calibration testing showed the stylometric
signal is unreliable on short text (see above); it still contributes a
quarter of the score so a strong disagreement can pull the result.

Thresholds are asymmetric on purpose: a false positive (flagging a human's
work as AI) is worse than a false negative on a creative platform, so it's
deliberately harder to land on "likely_ai" than on "likely_human":

| combined_score | attribution |
|---|---|
| >= 0.70 | `likely_ai` |
| <= 0.30 | `likely_human` |
| otherwise | `uncertain` |

`confidence` shown to the user is **not** the raw combined score -- it's
distance from the 0.5 midpoint, rescaled: `confidence = abs(combined_score - 0.5) * 2`.
This is how a 0.95 combined score and a 0.55 combined score produce visibly
different label confidence (90% vs 10%) instead of both just rounding to
"yes/no AI."

**Validating it's meaningful:** ran three real submissions through the live system (Groq + stylometric signals both active):

| input | llm_score | stylometric_score | combined | attribution | confidence |
|---|---|---|---|---|---|
| Descriptive personal narrative (porch scene) | 0.20 | 0.4969 | 0.2742 | `likely_human` | 45% |
| Formal AI-style paragraph ("transformative paradigm shift...") | 0.80 | 0.1306 | 0.6327 | `uncertain` | 27% |
| Casual human review (ramen post) | 0.20 | 0.00 | 0.15 | `likely_human` | 70% |

The casual review and the descriptive narrative both correctly land as
`likely_human`, but at different confidence levels (70% vs 45%) -- showing
the system doesn't collapse to a flat "human/not human" binary even within
one attribution. The formal AI-style paragraph is the interesting case: the
LLM signal alone scored it fairly AI-like (0.80), but because that wasn't
backed up by the stylometric signal, the asymmetric thresholds held the
combined verdict at "uncertain" rather than confidently flagging it --
exactly the false-positive-averse behavior the threshold design was meant
to produce. It also surfaced a real limitation in the threshold calibration
(see "Known limitations"): with only moderate signal agreement, even a
text that reads as clearly AI-generated to a human eye doesn't clear the
0.70 bar for `likely_ai`.

## Transparency label

Exact text, by attribution (`{confidence_pct}` is the rounded `confidence * 100`):

| Variant | Text |
|---|---|
| **High-confidence AI** | "This content has been flagged as likely AI-generated by our automated detection system (confidence: {confidence_pct}%). This is an automated assessment and may be incorrect. If you wrote this yourself, you can appeal this classification." |
| **High-confidence human** | "Our system found no strong signals of AI generation in this content (confidence: {confidence_pct}%). This reflects an automated assessment, not a verified guarantee of authorship." |
| **Uncertain** | "Our system could not confidently determine whether this content is AI-generated or human-written (confidence: {confidence_pct}%). Treat this classification as inconclusive." |

## Rate limiting

`5 per minute; 50 per day`, applied per-IP via Flask-Limiter.

Reasoning: a creator submitting their own work realistically posts a
handful of pieces in one sitting -- 5/minute comfortably covers that while
making a flood script hit 429s almost immediately. 50/day caps sustained
abuse or a misbehaving retry loop without blocking a genuinely prolific
creator across a full day.

Verified with the 12-rapid-request test:

```
200
200
200
200
200
429
429
429
429
429
429
429
```

## Audit log

Every `/submit` call writes one row capturing: `content_id`, `creator_id`,
`timestamp`, `llm_score` + reasoning, `stylometric_score` + detail,
`combined_score`, `attribution`, `confidence`, `label`, and `status`. An
appeal updates that same row's `status` and adds `appeal_reasoning` +
`appeal_timestamp` -- so the full history of a piece of content (original
decision + any appeal) lives on one queryable record. View via `GET /log`.


```json
{
    "appeal_reasoning": "I wrote this myself from personal experience sitting on my porch.",
    "appeal_timestamp": "2026-06-30T22:59:08.286414+00:00",
    "attribution": "likely_human",
    "combined_score": 0.2742,
    "confidence": 0.4516,
    "content_id": "2e27c45a-2471-4564-af2e-d83b0bc1e62f",
    "creator_id": "test-user-1",
    "label": "Our system found no strong signals of AI generation in this content (confidence: 45%). This reflects an automated assessment, not a verified guarantee of authorship.",
    "llm_score": 0.2,
    "status": "under_review",
    "stylometric_score": 0.4969,
    "timestamp": "2026-06-30T22:58:12.841494+00:00"
}

```

## Appeals workflow

`POST /appeal` with `content_id` + `creator_reasoning`. Looks up the
original submission, returns 404 if not found, otherwise sets
`status = "under_review"`, logs the reasoning and a timestamp on the same
row, and returns a confirmation. No automated re-classification -- a human
reviewer is expected to look at the original text, both signal scores, and
the creator's stated reasoning side by side via `GET /log`.

## Known limitations

The stylometric signal is unreliable on short submissions (roughly under
100 words / 4-5 sentences). At that length, type-token ratio is inflated
because there's no room for natural word repetition yet, and sentence-count
is too low for variance to mean anything. This was confirmed directly: all
four reference test inputs scored within 0.06 of each other on this signal
alone despite being intuitively very different registers. The system
compensates by weighting the LLM signal at 0.75, but that means short
submissions are effectively single-signal in practice, undercutting the
"at least 2 distinct signals" intent of the pipeline for exactly the
content type (short-form social posts, captions) where AI detection might
matter most.

Calibration testing also showed the "likely_ai" threshold (0.70) is harder
to clear than expected in practice: a paragraph that reads as clearly
AI-generated to a human eye scored 0.80 on the LLM signal alone, but
because the stylometric signal didn't agree as strongly, the combined
score (0.63) landed in "uncertain" rather than "likely_ai." This is the
false-positive-averse design working as intended, but it means confidently
flagging AI content requires fairly strong agreement between both signals
-- a single strong signal isn't enough to cross the bar alone.

## Spec reflection

The spec's insistence on writing out the exact label text *before* writing
any scoring code was the most useful constraint -- it forced a decision
about what "uncertain" should communicate to a non-technical reader before
there was a number to plug into it, which made the threshold design (asymmetric,
biased against false positives) an explicit choice rather than something
that fell out of whatever the math happened to produce.

Where implementation diverged from planning.md: the initial signal-weighting
plan was 0.55 LLM / 0.45 stylometric, on the assumption both signals would
contribute comparably. Testing against the four reference inputs showed
this under-classified an obviously-AI-generated sample as "uncertain"
because the stylometric signal dragged the score down on short text.
Reweighted to 0.75/0.25 after diagnosing that the stylometric signal itself
was the problem, not the threshold values -- documented in planning.md and
above under "Known limitations" rather than silently patched.

## AI usage

1. **Directed an AI tool to generate the initial `scoring.py`** (combine +
   classify + label logic) from the "Uncertainty representation" and
   "Transparency label design" sections of planning.md. It produced a
   working first pass with 0.55/0.45 weights. I overrode the weights to
   0.75/0.25 after testing against the four reference inputs revealed the
   original weighting mis-classified a clearly-AI sample as "uncertain" --
   the AI tool had implemented the spec's combination idea correctly, but
   had no way to know the stylometric signal would be this noisy on short
   text, since that only showed up empirically.

2. **Directed an AI tool to draft the Flask route skeleton** (`/submit`,
   `/appeal`, `/log`) from the architecture diagram. It initially had the
   LLM signal call raise an uncaught exception on API failure, which would
   have taken down the whole `/submit` endpoint if Groq had a hiccup. I
   rewrote that section to catch the exception and fail toward `score=0.5`
   ("uncertain") instead, consistent with the project's overall stance that
   the system should fail toward acknowledged uncertainty rather than a
   confident-looking guess or a crash.
