"""
Provenance Guard -- Flask backend.

Endpoints:
  POST /submit  -- classify a piece of text, return attribution + label
  POST /appeal  -- contest a classification
  GET  /log     -- structured audit log (most recent entries)
"""

import os
import uuid
import json

from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

import db
import scoring
from signals import stylometric_signal, llm_signal

load_dotenv()

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
# 5 per minute / 50 per day, applied per-IP.
# Reasoning (see README "Rate limiting" for the full writeup):
#   - A single creator submitting their own work realistically submits a
#     handful of pieces in a sitting, not dozens per minute -- 5/min covers
#     that comfortably while making a flood-the-endpoint script land on 429s
#     almost immediately.
#   - 50/day caps sustained abuse (or a misbehaving integration retrying in
#     a loop) without blocking a genuinely prolific creator across a whole day.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

db.init_db()


@app.route("/submit", methods=["POST"])
@limiter.limit("5 per minute;50 per day")
def submit():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    creator_id = data.get("creator_id", "").strip()

    if not text or not creator_id:
        return jsonify({"error": "both 'text' and 'creator_id' are required"}), 400

    content_id = str(uuid.uuid4())
    timestamp = db.now_iso()

    # Signal 1: stylometric (always available, no network)
    sty = stylometric_signal(text)
    stylometric_score = sty["ai_likelihood"]

    # Signal 2: LLM (Groq). If the API key isn't configured or the call
    # fails, fail toward "uncertain" rather than crash the endpoint --
    # a creative platform's submission endpoint shouldn't go down because
    # an upstream API hiccupped.
    try:
        llm = llm_signal(text)
        llm_score = llm["ai_likelihood"]
        llm_reasoning = llm.get("reasoning", "")
    except Exception as e:
        llm_score = 0.5
        llm_reasoning = f"llm_signal unavailable: {e}"

    result = scoring.score_text(llm_score, stylometric_score)

    row = {
        "content_id": content_id,
        "creator_id": creator_id,
        "timestamp": timestamp,
        "text": text,
        "llm_score": llm_score,
        "llm_reasoning": llm_reasoning,
        "stylometric_score": stylometric_score,
        "stylometric_detail": json.dumps(sty),
        "combined_score": result["combined_score"],
        "attribution": result["attribution"],
        "confidence": result["confidence"],
        "label": result["label"],
        "status": "classified",
    }
    db.insert_submission(row)

    return jsonify({
        "content_id": content_id,
        "attribution": result["attribution"],
        "confidence": result["confidence"],
        "label": result["label"],
        "signals": {
            "llm_score": llm_score,
            "stylometric_score": stylometric_score,
        },
    })


@app.route("/appeal", methods=["POST"])
def appeal():
    data = request.get_json(silent=True) or {}
    content_id = data.get("content_id", "").strip()
    creator_reasoning = data.get("creator_reasoning", "").strip()

    if not content_id or not creator_reasoning:
        return jsonify({"error": "both 'content_id' and 'creator_reasoning' are required"}), 400

    existing = db.get_submission(content_id)
    if not existing:
        return jsonify({"error": "no submission found with that content_id"}), 404

    updated = db.file_appeal(content_id, creator_reasoning)
    if not updated:
        return jsonify({"error": "appeal could not be recorded"}), 500

    return jsonify({
        "content_id": content_id,
        "status": "under_review",
        "message": "Appeal received and logged. A human reviewer will examine "
                    "the original classification alongside your reasoning.",
    })


@app.route("/log", methods=["GET"])
def log():
    limit = int(request.args.get("limit", 50))
    entries = db.get_log(limit=limit)
    return jsonify({"entries": entries})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
