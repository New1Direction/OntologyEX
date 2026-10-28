"""A local System One endpoint for tests and offline demos. NOT a decision model.

It validates requests like the TypeSafe API (model, state, typed questions) and
answers with deterministic word overlap, or with a handler a test supplies. It lets
the real HTTP client, batching, caching, and every integration run without a key.
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

WORD = re.compile(r"[a-z0-9]+")
STOP = {"the", "a", "an", "of", "to", "is", "this", "that", "does", "do", "these", "two",
        "it", "or", "and", "in", "on", "for", "by", "be", "can", "which", "with", "as",
        "listed", "code", "binding", "runs", "serves", "statement", "evidence", "support",
        "rule", "written", "each", "other", "way", "one", "more", "than", "read"}


def words(text: str) -> set[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    return {w for w in WORD.findall(spaced.lower()) if len(w) > 1 and w not in STOP}


def overlap_answers(state, questions: dict) -> dict:
    text = state if isinstance(state, str) else json.dumps(state)
    have = words(text)
    answers = {}
    for qid, q in questions.items():
        if q["type"] == "noul":
            want = words(q["instructions"]) or {"_"}
            answers[qid] = {"type": "noul", "noul": round(len(want & have) / len(want), 4)}
        elif q["type"] == "choice":
            raw = {k: len(words(k + " " + v) & have) + (0.5 if k == "none" else 0)
                   for k, v in q["criteria"].items()}
            total = sum(raw.values()) or 1
            probs = {k: round(v / total, 4) for k, v in raw.items()}
            best = max(sorted(probs), key=lambda k: probs[k])
            answers[qid] = {"type": "choice", "choice": best, "confidence": probs[best],
                            "probabilities": probs}
        else:
            hits = len(words(q["instructions"]) & have)
            level = min(len(q["criteria"]) - 1, hits)
            answers[qid] = {"type": "score", "score": float(level), "confidence": 0.5,
                            "legend": {str(i): c for i, c in enumerate(q["criteria"])},
                            "probabilities": {str(level): 1.0}}
    return answers


class FakeSystemOne:
    def __init__(self, handler=overlap_answers, cloudflare_envelope: bool = False, token: str = ""):
        self.handler = handler
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if token and self.headers.get("Authorization") != f"Bearer {token}":
                    return self.reply(401, {"detail": "unauthorized"})
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                problems = outer.validate(body)
                if problems:
                    return self.reply(422, {"detail": [{"loc": ["body"], "msg": p} for p in problems]})
                outer.requests.append(body)
                result = {"model": body["model"], "answers": outer.handler(body["state"], body["questions"]),
                          "usage": {"input_tokens": len(json.dumps(body)) // 4, "output_tokens": 0}}
                if cloudflare_envelope:
                    result = {"result": result, "success": True, "errors": [], "messages": []}
                return self.reply(200, result)

            def reply(self, code, data):
                raw = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/systemone"
        Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def validate(body) -> list[str]:
        problems = []
        if type(body) is not dict or set(body) - {"model", "state", "questions", "assets"}:
            return ["unexpected request fields"]
        if type(body.get("model")) is not str:
            problems.append("model required")
        if type(body.get("state")) not in (str, dict, list):
            problems.append("state required")
        questions = body.get("questions")
        if type(questions) is not dict or not questions or len(questions) > 64:
            return problems + ["1..64 questions required"]
        for qid, q in questions.items():
            if q.get("type") not in ("noul", "choice", "score"):
                problems.append(f"{qid}: bad type")
            elif q["type"] == "choice" and not (type(q.get("criteria")) is dict and 0 < len(q["criteria"]) <= 255):
                problems.append(f"{qid}: choice criteria")
            elif q["type"] == "score" and not (type(q.get("criteria")) is list and 0 < len(q["criteria"]) <= 10):
                problems.append(f"{qid}: score criteria")
        return problems

    def close(self):
        self.server.shutdown()
