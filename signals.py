"""
Detection signals for Provenance Guard.

Two independent signals, as required:
1. stylometric_signal() — pure-Python structural heuristics (no network).
2. llm_signal()         — Groq-based semantic/stylistic judgment (network call).

Both return a dict: {"ai_likelihood": float in [0,1], ...supporting detail}
where higher = more AI-like. Keeping the output shape identical makes them
easy to combine in scoring.py regardless of how each one computes its score.
"""

import os
import re
import json
import statistics

# ---------------------------------------------------------------------------
# Signal 1: Stylometric heuristics (structural, no external calls)
# ---------------------------------------------------------------------------
# What it measures: sentence-length variance and vocabulary diversity
# (type-token ratio). AI-generated text tends toward more uniform sentence
# lengths and somewhat repetitive phrasing; human writing tends to be more
# irregular — varied sentence length, idiosyncratic word choice.
#
# Blind spot: this is a population-level tendency, not a law. Short texts
# (under ~50 words) don't give variance/TTR enough room to be meaningful,
# and deliberately plain or repetitive human writing (children's writing,
# minimalist styles, certain poetry) will score AI-like even though it
# isn't. Heavily edited AI text that has had its uniformity intentionally
# broken up will also slip past this signal.

def _sentences(text: str):
    parts = re.split(r"[.!?]+", text)
    return [p.strip() for p in parts if p.strip()]


def _words(text: str):
    return re.findall(r"[A-Za-z']+", text.lower())


def stylometric_signal(text: str) -> dict:
    sentences = _sentences(text)
    words = _words(text)

    if len(sentences) < 2 or len(words) < 10:
        # Not enough signal to say anything meaningful — return a neutral
        # midpoint rather than a confident-looking number.
        return {
            "ai_likelihood": 0.5,
            "sentence_length_variance": None,
            "type_token_ratio": None,
            "note": "insufficient text length for reliable stylometric scoring",
        }

    sentence_lengths = [len(_words(s)) for s in sentences]
    variance = statistics.pvariance(sentence_lengths) if len(sentence_lengths) > 1 else 0.0

    ttr = len(set(words)) / len(words)

    # Normalize. These constants are heuristic, calibrated against the
    # sample inputs in planning.md (see "Calibration" note there).
    norm_var = min(variance / 40.0, 1.0)      # >=40 word^2 variance treated as "fully human-variable"
    norm_ttr = min(ttr / 0.70, 1.0)           # TTR >= 0.70 treated as "fully diverse vocabulary"

    ai_likelihood = 0.5 * (1 - norm_var) + 0.5 * (1 - norm_ttr)
    ai_likelihood = max(0.0, min(1.0, ai_likelihood))

    return {
        "ai_likelihood": round(ai_likelihood, 4),
        "sentence_length_variance": round(variance, 2),
        "type_token_ratio": round(ttr, 4),
    }


# ---------------------------------------------------------------------------
# Signal 2: LLM-based holistic judgment (Groq)
# ---------------------------------------------------------------------------
# What it measures: semantic and stylistic coherence as judged holistically
# by a language model — the kind of "this reads off" intuition that's hard
# to reduce to a formula.
#
# Blind spot: it's a black box — we don't know exactly what cues it's using,
# so its mistakes are hard to predict or explain. It can also be confidently
# wrong, and it inherits whatever biases the underlying model has about what
# "good AI writing" or "good human writing" looks like.

_GROQ_MODEL = "llama-3.3-70b-versatile"

_PROMPT = """You are assisting a content-attribution system. Assess whether \
the following piece of text was most likely written by a human or generated \
by an AI language model.

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"ai_likelihood": <float 0.0-1.0, where 1.0 = certainly AI-generated, \
0.0 = certainly human-written>, "reasoning": "<one sentence>"}}

Text to assess:
---
{text}
---
"""


def llm_signal(text: str) -> dict:
    """Calls Groq. Requires GROQ_API_KEY in the environment."""
    try:
        from groq import Groq
    except ImportError as e:
        raise RuntimeError("groq package not installed — pip install groq") from e

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY not set in environment")

    client = Groq(api_key=api_key)
    completion = client.chat.completions.create(
        model=_GROQ_MODEL,
        messages=[{"role": "user", "content": _PROMPT.format(text=text)}],
        temperature=0.0,
        max_tokens=200,
    )

    raw = completion.choices[0].message.content.strip()
    raw = re.sub(r"^```json|```$", "", raw, flags=re.MULTILINE).strip()

    try:
        parsed = json.loads(raw)
        ai_likelihood = float(parsed.get("ai_likelihood", 0.5))
        ai_likelihood = max(0.0, min(1.0, ai_likelihood))
        return {
            "ai_likelihood": round(ai_likelihood, 4),
            "reasoning": parsed.get("reasoning", ""),
        }
    except (json.JSONDecodeError, TypeError, ValueError):
        # Model didn't return clean JSON — fail toward "uncertain", not toward
        # a confident-looking guess. Logged so it's visible in the audit trail.
        return {
            "ai_likelihood": 0.5,
            "reasoning": "llm_signal: could not parse model output",
            "raw_output": raw[:200],
        }
