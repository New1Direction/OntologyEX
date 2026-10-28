#!/usr/bin/env python3
"""Decision-model client for Jev (TypeSafe) and Clef / Clef-flash (Cloudflare).

Both speak the System One API: a `state` plus typed `questions` (noul = yes/no,
choice, score) answered with probabilities instead of text. OntologyEX uses them
only to *propose*: link suggestions, review triage, source proposals, conflict
candidates, wording warnings, and command routing in the business page. A model
answer never confirms a link, records a review, changes health, or authorizes
anything; the proven paths (rollup, grades, link decisions, fusion) never call it.

Opt-in. Configure with environment variables (never files):

    ONTOLOGYEX_DECIDE_PROVIDER   typesafe | cloudflare | system-one
    ONTOLOGYEX_DECIDE_MODEL      jev-latest | clef | clef-flash | <name>
    ONTOLOGYEX_DECIDE_TOKEN      API key / token
    CLOUDFLARE_ACCOUNT_ID        for cloudflare
    ONTOLOGYEX_DECIDE_URL        for system-one: full URL of a compatible endpoint
                                 (a self-hosted Clef, a gateway, or a test server)

What you send leaves your machine: callers pass only the text they need (a code
binding's quote, a file preview, a rule and its evidence). Answers are cached by a
digest of provider, model, state, and questions under ONTOLOGYEX_DECIDE_CACHE
(default `.ontologyex-decisions/`), so a repeated question costs nothing.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request

PROVIDERS = ("typesafe", "cloudflare", "system-one")
DEFAULT_MODEL = {"typesafe": "jev-latest", "cloudflare": "clef-flash", "system-one": "clef-flash"}
MAX_QUESTIONS = 64          # Clef's per-request limit; batches above it
MAX_CHOICES = 255           # Jev's choice limit
MAX_SCORE_LEVELS = 10
MAX_STATE_CHARS = 60_000    # well inside 32K (Jev) / 64K (Clef) tokens
MAX_RESPONSE = 1024 * 1024
QUESTION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class DecisionError(RuntimeError):
    """The decision model is not configured, refused the request, or answered badly."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _check(ok: bool, message: str) -> None:
    if not ok:
        raise DecisionError(message)


def validate_questions(questions: dict) -> None:
    _check(type(questions) is dict and 0 < len(questions), "questions: expected a non-empty object")
    for qid, q in questions.items():
        _check(QUESTION_ID.fullmatch(qid) is not None, f"invalid question id {qid!r}")
        _check(type(q) is dict and q.get("type") in ("noul", "choice", "score"), f"{qid}: bad type")
        _check(type(q.get("instructions")) is str and 0 < len(q["instructions"]) <= 2000,
               f"{qid}: instructions required")
        if q["type"] == "choice":
            c = q.get("criteria")
            _check(type(c) is dict and 0 < len(c) <= MAX_CHOICES and
                   all(type(k) is str and 0 < len(k) <= 100 and type(v) is str and len(v) <= 2000
                       for k, v in c.items()), f"{qid}: choice criteria must map 1..255 names to text")
        elif q["type"] == "score":
            c = q.get("criteria")
            _check(type(c) is list and 0 < len(c) <= MAX_SCORE_LEVELS and
                   all(type(x) is str and 0 < len(x) <= 2000 for x in c),
                   f"{qid}: score criteria must be 1..10 levels")


def _probability(value: object, where: str) -> float:
    _check(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1,
           f"{where}: expected a probability")
    return float(value)


def validate_answers(questions: dict, answers: object) -> dict:
    """Keep only well-formed answers to the questions asked; anything else is an error."""
    _check(type(answers) is dict, "answers: expected an object")
    clean = {}
    for qid, q in questions.items():
        a = answers.get(qid)
        _check(type(a) is dict, f"missing answer for {qid}")
        if q["type"] == "noul":
            clean[qid] = {"type": "noul", "noul": _probability(a.get("noul"), qid)}
        elif q["type"] == "choice":
            probs = a.get("probabilities", {})
            _check(a.get("choice") in q["criteria"], f"{qid}: answer is not one of the choices")
            _check(type(probs) is dict and set(probs) <= set(q["criteria"]), f"{qid}: bad probabilities")
            clean[qid] = {"type": "choice", "choice": a["choice"],
                          "confidence": _probability(a.get("confidence", probs.get(a["choice"], 0)), qid),
                          "probabilities": {k: _probability(v, qid) for k, v in probs.items()}}
        else:
            score = a.get("score")
            _check(type(score) in (int, float) and math.isfinite(score) and
                   0 <= score <= len(q["criteria"]) - 1, f"{qid}: score out of range")
            clean[qid] = {"type": "score", "score": float(score),
                          "confidence": _probability(a.get("confidence", 0), qid)}
    return clean


class DecisionClient:
    """Synchronous System One client with batching, caching, and strict validation."""

    def __init__(self, provider: str | None = None, model: str | None = None,
                 token: str | None = None, url: str | None = None,
                 cache: Path | None = None, timeout: float = 20.0):
        env = os.environ
        self.provider = provider or env.get("ONTOLOGYEX_DECIDE_PROVIDER", "")
        _check(self.provider in PROVIDERS,
               "decision model not configured: set ONTOLOGYEX_DECIDE_PROVIDER "
               "(typesafe, cloudflare, or system-one)")
        self.model = model or env.get("ONTOLOGYEX_DECIDE_MODEL") or DEFAULT_MODEL[self.provider]
        self.token = token if token is not None else env.get("ONTOLOGYEX_DECIDE_TOKEN", "")
        if self.provider == "typesafe":
            self.url = "https://api.typesafe.ai/v1/systemone"
        elif self.provider == "cloudflare":
            account = env.get("CLOUDFLARE_ACCOUNT_ID", "")
            _check(re.fullmatch(r"[0-9a-f]{32}", account) is not None, "set CLOUDFLARE_ACCOUNT_ID")
            _check(self.model in ("clef", "clef-flash"), "cloudflare models are clef and clef-flash")
            self.url = (f"https://api.cloudflare.com/client/v4/accounts/{account}"
                        f"/ai/run/@cf/cloudflare/{self.model}")
        else:
            self.url = url or env.get("ONTOLOGYEX_DECIDE_URL", "")
            _check(self.url.startswith(("https://", "http://127.0.0.1", "http://localhost")),
                   "system-one needs ONTOLOGYEX_DECIDE_URL (https, or http on loopback)")
        _check(self.provider == "system-one" or bool(self.token), "set ONTOLOGYEX_DECIDE_TOKEN")
        self.cache = Path(cache or env.get("ONTOLOGYEX_DECIDE_CACHE", ".ontologyex-decisions"))
        self.timeout = timeout
        self.calls = 0
        self.cache_hits = 0
        self._opener = urllib.request.build_opener(_NoRedirect)

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    def _key(self, state, questions) -> str:
        blob = json.dumps([self.provider, self.model, state, questions], sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _post(self, state, questions: dict) -> dict:
        body = json.dumps({"model": self.model, "state": state, "questions": questions},
                          ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "ontologyex-decide/1"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", "replace") if exc.fp else ""
            raise DecisionError(f"decision model answered HTTP {exc.code}: {detail[:300]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise DecisionError(f"decision model unreachable: {type(exc).__name__}") from exc
        _check(len(raw) <= MAX_RESPONSE, "decision response too large")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise DecisionError("decision response is not JSON") from exc
        if type(data) is dict and "result" in data and "answers" not in data:  # Cloudflare envelope
            _check(data.get("success", True) is not False, "cloudflare reported failure")
            data = data["result"]
        _check(type(data) is dict, "decision response is not an object")
        return validate_answers(questions, data.get("answers"))

    def decide(self, state, questions: dict) -> dict:
        """Answers keyed like `questions`. Batches above 64 questions; caches each batch."""
        validate_questions(questions)
        if type(state) is str:
            state = state[:MAX_STATE_CHARS]
        else:
            _check(len(json.dumps(state, ensure_ascii=False)) <= MAX_STATE_CHARS, "state too large")
        ids = list(questions)
        answers = {}
        for start in range(0, len(ids), MAX_QUESTIONS):
            batch = {qid: questions[qid] for qid in ids[start:start + MAX_QUESTIONS]}
            key = self._key(state, batch)
            path = self.cache / key[:2] / f"{key}.json"
            if path.is_file() and not path.is_symlink():
                try:
                    answers.update(validate_answers(batch, json.loads(path.read_text("utf-8"))))
                    self.cache_hits += 1
                    continue
                except (DecisionError, ValueError, OSError):
                    pass  # a damaged cache entry is asked again
            result = self._post(state, batch)
            self.calls += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
                json.dump(result, handle, sort_keys=True)
            Path(handle.name).replace(path)
            answers.update(result)
        return answers

    def decide_many(self, requests: list[tuple[object, dict]], workers: int = 8) -> list[dict]:
        """Independent (state, questions) requests in parallel, results in order."""
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as pool:
            return list(pool.map(lambda pair: self.decide(*pair), requests))


def from_env() -> DecisionClient | None:
    """A configured client, or None when no provider is set (model assist is off)."""
    if not os.environ.get("ONTOLOGYEX_DECIDE_PROVIDER"):
        return None
    return DecisionClient()


def describe(client: DecisionClient | None) -> dict:
    if client is None:
        return {"status": "OFF"}
    return {"status": "ON", "model": client.label, "calls": client.calls,
            "cache_hits": client.cache_hits,
            "semantics": "MODEL_PROBABILITIES_ARE_PROPOSALS_NOT_DECISIONS"}
