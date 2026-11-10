#!/usr/bin/env python3
"""
Search eval — does the search find the visits an operator would call right?

A relevance-labelled set of queries against ONE site's own visits, scored
by rank: for each query the ids of the visits that answer it (judged from
their captions and, where it mattered, their evidence frames), and what
``GET /api/v1/search`` returned for the same words. Reports hit@5,
recall@10 and the reciprocal rank of the first right answer per case and
averaged, plus which arm put each hit there (the ``semantic`` block), so a
change to the floor, the fusion, the embedder or the parser is judged by
a number rather than by reading result tables.

The cases are a site's: the ids and the window belong to the box they
were labelled on (``bench/search-eval-cases-*.yml``). Run it on that box
(or through a tunnel to it):

    python3 tools/search_eval.py --cases bench/search-eval-cases-ventuno-q-2026-11-10.yml --login
    python3 tools/search_eval.py --cases ... --token-file /tmp/search-eval.tokens --out /tmp/search-eval.json

``--login`` asks for an operator login (nothing on the command line) and,
with ``--token-file``, keeps the access + refresh tokens there (mode 0600)
so later runs need no login until the refresh token expires. Negative
cases (``relevant: []``) pass when the search returns nothing, or says it
relaxed the words away (``relaxed``), or every hit the vector arms kept
is under the floor (``semantic.reason == below-floor``).

Case file:

    window: {from: "2026-11-05T11:30:00Z"}      # applied to every case unless it has its own
    cases:
      - query: man in a yellow shirt
        relevant: [17932, 17929]
        kind: caption                             # caption | paraphrase | typo | negative | attribute
        note: Nov 7, three visitors
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


# ── auth ──────────────────────────────────────────────────────────────


def _post_json(url: str, body: dict, headers: dict | None = None, timeout: float = 30) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def login(base: str) -> dict:
    user = input("OpenNVR username: ")
    password = getpass.getpass("OpenNVR password: ")
    code = input("MFA code (6 digits, blank if MFA is off): ").strip()
    body = {"username": user, "password": password}
    if code:
        body["code"] = code
    answer = _post_json(base + "/api/v1/auth/login-json", body)
    del password
    if not answer.get("access_token"):
        sys.exit("login: no access_token in the answer (keys: " + ", ".join(answer) + ")")
    return {"access_token": answer["access_token"],
            "refresh_token": answer.get("refresh_token") or "",
            "device_token": answer.get("device_token") or ""}


def refresh(base: str, tokens: dict) -> bool:
    if not tokens.get("refresh_token"):
        return False
    try:
        answer = _post_json(base + "/api/v1/auth/refresh", {"refresh_token": tokens["refresh_token"]})
    except Exception as exc:  # noqa: BLE001
        print(f"token refresh failed: {exc}", file=sys.stderr)
        return False
    if not answer.get("access_token"):
        return False
    tokens["access_token"] = answer["access_token"]
    tokens["refresh_token"] = answer.get("refresh_token") or tokens["refresh_token"]
    return True


def save_tokens(path: str, tokens: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(tokens, fh)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


# ── the search ────────────────────────────────────────────────────────


class Searcher:
    def __init__(self, base: str, tokens: dict, token_file: str | None, limit: int) -> None:
        self.base = base.rstrip("/")
        self.tokens = tokens
        self.token_file = token_file
        self.limit = limit

    def _headers(self) -> dict:
        h = {"Authorization": "Bearer " + self.tokens["access_token"]}
        if self.tokens.get("device_token"):
            h["X-Device-Token"] = self.tokens["device_token"]
        return h

    def search(self, query: str, window: dict | None, params: dict | None = None) -> tuple[dict, float]:
        q: dict[str, Any] = {"q": query, "limit": self.limit, "describe": "false"}
        if window:
            if window.get("from"):
                q["from"] = window["from"]
            if window.get("to"):
                q["to"] = window["to"]
        q.update(params or {})
        url = self.base + "/api/v1/search?" + urllib.parse.urlencode(q, doseq=True)
        for attempt in (1, 2):
            req = urllib.request.Request(url, headers=self._headers())
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read()), (time.perf_counter() - t0) * 1000
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 1 and refresh(self.base, self.tokens):
                    if self.token_file:
                        save_tokens(self.token_file, self.tokens)
                    continue
                raise
        raise RuntimeError("unreachable")


# ── scoring ───────────────────────────────────────────────────────────


def score_case(case: dict, answer: dict) -> dict:
    hits = [int(h["id"]) for h in answer.get("hits") or []]
    relevant = {int(i) for i in case.get("relevant") or []}
    sem = answer.get("semantic") or {}
    out: dict[str, Any] = {
        "query": case["query"], "kind": case.get("kind") or "caption",
        "returned": len(hits), "total": answer.get("total"),
        "relevant": len(relevant),
        "corrections": (answer.get("interpretation") or {}).get("corrections"),
        "relaxed": bool(answer.get("relaxed")),
        "semantic": {k: sem.get(k) for k in ("used", "reason", "arms", "added_by_vector",
                                             "added_by_caption", "best", "floor")} if sem else None,
        "caption_best": ((sem.get("caption") or {}).get("best") if sem else None),
    }
    if not relevant:
        # A negative: nothing should come back as a confident answer.
        empty = not hits
        relaxed = bool(answer.get("relaxed"))
        below = sem.get("reason") == "below-floor"
        out.update(passed=(empty or relaxed or below), top=hits[:5])
        return out
    first = next((i for i, h in enumerate(hits) if h in relevant), None)
    top5 = [h for h in hits[:5] if h in relevant]
    top10 = [h for h in hits[:10] if h in relevant]
    out.update(
        first_rank=(None if first is None else first + 1),
        rr=(0.0 if first is None else 1.0 / (first + 1)),
        hit5=bool(top5),
        p5=len(top5) / min(5, max(1, len(hits[:5]))) if hits else 0.0,
        recall10=len(top10) / len(relevant),
        recall_all=len([h for h in hits if h in relevant]) / len(relevant),
        wrong_top5=[h for h in hits[:5] if h not in relevant],
    )
    out["passed"] = bool(top5)
    return out


def summarise(results: list[dict]) -> dict:
    pos = [r for r in results if r["relevant"]]
    neg = [r for r in results if not r["relevant"]]
    by_kind: dict[str, list[dict]] = {}
    for r in pos:
        by_kind.setdefault(r["kind"], []).append(r)

    def _avg(rows: list[dict], key: str) -> float | None:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    summary = {
        "cases": len(results), "positive": len(pos), "negative": len(neg),
        "mrr": _avg(pos, "rr"), "hit@5": _avg(pos, "hit5"), "p@5": _avg(pos, "p5"),
        "recall@10": _avg(pos, "recall10"), "recall@limit": _avg(pos, "recall_all"),
        "negatives_passed": sum(1 for r in neg if r["passed"]),
        "by_kind": {k: {"n": len(v), "mrr": _avg(v, "rr"), "hit@5": _avg(v, "hit5"),
                        "recall@10": _avg(v, "recall10")} for k, v in sorted(by_kind.items())},
        "arms_used": sum(1 for r in results if (r.get("semantic") or {}).get("used")),
    }
    return summary


def print_table(results: list[dict], summary: dict, ms: list[float]) -> None:
    print(f"{'query':44s} {'kind':10s} {'rel':>3s} {'1st':>4s} {'r@10':>5s} {'hit5':>4s}  arms")
    for r in results:
        sem = r.get("semantic") or {}
        arms = sem.get("arms") or {}
        arms_s = ",".join(f"{k}:{v}" for k, v in arms.items()) if arms else (sem.get("reason") or "-")
        if r["relevant"]:
            print(f"{r['query'][:44]:44s} {r['kind']:10s} {r['relevant']:3d} "
                  f"{(r['first_rank'] or '-')!s:>4s} {r['recall10']:5.2f} {'yes' if r['hit5'] else 'NO':>4s}  {arms_s}")
        else:
            print(f"{r['query'][:44]:44s} {r['kind']:10s}   0 {'-':>4s} {'-':>5s} "
                  f"{'ok' if r['passed'] else 'NO':>4s}  {arms_s} top={r['top']}")
    print()
    print(f"cases {summary['cases']} (positive {summary['positive']}, negative {summary['negative']}): "
          f"MRR {summary['mrr']}  hit@5 {summary['hit@5']}  P@5 {summary['p@5']}  "
          f"recall@10 {summary['recall@10']}  negatives passed {summary['negatives_passed']}/{summary['negative']}")
    for k, v in summary["by_kind"].items():
        print(f"  {k:10s} n={v['n']:2d}  MRR {v['mrr']}  hit@5 {v['hit@5']}  recall@10 {v['recall@10']}")
    if ms:
        ms_sorted = sorted(ms)
        print(f"latency ms: median {ms_sorted[len(ms)//2]:.0f}  p90 {ms_sorted[int(len(ms)*0.9)-1 if len(ms) > 1 else 0]:.0f}  max {ms_sorted[-1]:.0f}")


# ── main ──────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", required=True, help="YAML case file")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="OpenNVR server")
    ap.add_argument("--login", action="store_true", help="ask for an operator login")
    ap.add_argument("--token", default=os.environ.get("OPENNVR_TOKEN"), help="an access token")
    ap.add_argument("--token-file", help="where the login's tokens are kept / read from (0600)")
    ap.add_argument("--limit", type=int, default=25, help="hits to ask for per query")
    ap.add_argument("--out", help="write the full results as JSON here")
    ap.add_argument("--only", help="run only the cases whose query contains this text")
    args = ap.parse_args()

    if yaml is None:
        sys.exit("PyYAML is needed: pip install pyyaml")
    with open(args.cases, encoding="utf-8") as fh:
        spec = yaml.safe_load(fh) or {}
    cases = [c for c in spec.get("cases") or [] if not args.only or args.only in c["query"]]
    if not cases:
        sys.exit("no cases")

    tokens: dict | None = None
    if args.login:
        tokens = login(args.base)
        if args.token_file:
            save_tokens(args.token_file, tokens)
    elif args.token_file and os.path.exists(args.token_file):
        with open(args.token_file, encoding="utf-8") as fh:
            tokens = json.load(fh)
    elif args.token:
        tokens = {"access_token": args.token, "refresh_token": "", "device_token": ""}
    if not tokens:
        sys.exit("no credentials: --login, --token-file or --token")

    searcher = Searcher(args.base, tokens, args.token_file, args.limit)
    window = spec.get("window") or {}
    results, latencies = [], []
    for case in cases:
        answer, ms = searcher.search(case["query"], case.get("window") or window, case.get("params"))
        latencies.append(ms)
        r = score_case(case, answer)
        r["ms"] = round(ms)
        r["note"] = case.get("note")
        results.append(r)
    summary = summarise(results)
    print_table(results, summary, latencies)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"base": args.base, "cases_file": args.cases, "window": window,
                       "summary": summary, "results": results,
                       "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, fh, indent=1)
        print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
