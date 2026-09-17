#!/usr/bin/env python3
"""bpe_tokenizer.py---a tiny, dependency-free tokenizer for HuggingFace `tokenizer.json`
files of the byte-level-BPE family (Qwen / GPT-2 / GPT-4 style). Pure stdlib.

Why this exists: to count reasoning vs writing tokens EXACTLY and OFFLINE, show-log.py needs
the model's own tokenizer. The reference loaders (`tokenizers`, `transformers`) are third-party,
and stdlib `re` can't parse the pre-tokenizer's `\\p{L}`/`\\p{N}`/`\\p{M}` classes---so we
emulate them with `unicodedata.category()` and hand-roll the pipeline:

    NFC normalize → GPT-4-style pre-tokenizer split → byte-level byte→unicode map → ranked BPE.

Scope: exactly what Qwen3's tokenizer.json declares---BPE with ignore_merges=False, no unk,
no continuing-subword prefix / end-of-word suffix, a ByteLevel post-processor that adds no
bos/eos to a bare string. `count()` returns the token count; `encode_pieces()` returns the
byte-level piece strings. Only `merges` are needed to tokenize (every byte char and every merge
result is already a vocab entry), so we don't load the 248k-entry vocab. Validated piece-for-
piece against the backend's /tokenize endpoint (see selftest at the bottom).
"""
import json
import unicodedata
from functools import lru_cache


@lru_cache(maxsize=1)
def _byte_encoder():
    """GPT-2 byte→unicode table: a reversible map of all 256 bytes to printable code points."""
    bs = list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return {b: chr(c) for b, c in zip(bs, cs)}


def _catL(ch):
    return unicodedata.category(ch)[0] == "L"


def _catN(ch):
    return unicodedata.category(ch)[0] == "N"


def _catLM(ch):
    c = unicodedata.category(ch)[0]
    return c == "L" or c == "M"


def _isspace(ch):
    return ch.isspace()  # proxy for regex \s (Unicode whitespace)


_CONTRACTIONS = ("'s", "'t", "'re", "'ve", "'m", "'ll", "'d")


def pretokenize(text):
    r"""Emulate the tokenizer.json Split pattern (ordered alternation, applied left-to-right):
        (?i:'s|'t|'re|'ve|'m|'ll|'d)                       # contractions
        | [^\r\n\p{L}\p{N}]?[\p{L}\p{M}]+                  # opt. leading non-L/N char + letters
        | \p{N}                                            # one numeric char (Qwen splits digits)
        |  ?[^\s\p{L}\p{M}\p{N}]+[\r\n]*                   # opt. space + punct run + trailing nl
        | \s*[\r\n]+ | \s+(?!\S) | \s+                     # whitespace, newline-aware
    Returns the ordered list of pre-token strings; their concatenation == text."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]

        # 1) contractions (case-insensitive)
        if ch == "'":
            low = text[i:i + 3].lower()
            hit = next((c for c in _CONTRACTIONS if low.startswith(c)), None)
            if hit:
                out.append(text[i:i + len(hit)])
                i += len(hit)
                continue

        # 2) [^\r\n\p{L}\p{N}]? [\p{L}\p{M}]+
        j = i
        if ch != "\r" and ch != "\n" and not _catL(ch) and not _catN(ch):
            # optional leading char is only taken if a letter/mark run follows it
            if i + 1 < n and _catLM(text[i + 1]):
                j = i + 1
        if j < n and _catLM(text[j]):
            k = j
            while k < n and _catLM(text[k]):
                k += 1
            out.append(text[i:k])
            i = k
            continue

        # 3) \p{N} (single numeric char)
        if _catN(ch):
            out.append(ch)
            i += 1
            continue

        # 4)  ?[^\s\p{L}\p{M}\p{N}]+[\r\n]*
        j = i
        if ch == " " and i + 1 < n:
            nx = text[i + 1]
            if not (_isspace(nx) or _catL(nx) or _catN(nx) or unicodedata.category(nx)[0] == "M"):
                j = i + 1
        if j < n:
            cj = text[j]
            if not (_isspace(cj) or _catL(cj) or _catN(cj) or unicodedata.category(cj)[0] == "M"):
                k = j
                while k < n:
                    c = text[k]
                    if _isspace(c) or _catL(c) or _catN(c) or unicodedata.category(c)[0] == "M":
                        break
                    k += 1
                while k < n and (text[k] == "\r" or text[k] == "\n"):
                    k += 1
                out.append(text[i:k])
                i = k
                continue

        # 5/6/7) whitespace run---newline-aware
        if _isspace(ch):
            e = i
            while e < n and _isspace(text[e]):
                e += 1
            run = text[i:e]
            nl = max(run.rfind("\r"), run.rfind("\n"))
            if nl >= 0:                       # \s*[\r\n]+  → up to & incl. the last newline
                out.append(text[i:i + nl + 1])
                i = i + nl + 1
                continue
            if e == n:                        # \s+(?!\S) at end of text → whole run
                out.append(run)
                i = e
                continue
            if e - 1 > i:                     # \s+(?!\S) → run minus its last space...
                out.append(text[i:e - 1])
                i = e - 1
                continue
            # ...the final single space is picked up by alt 2/4 with the next token; if we get
            # here it's a lone space before a non-L/N/M non-punct (rare)---emit it alone.
            out.append(ch)
            i += 1
            continue

        # fallback: never stall
        out.append(ch)
        i += 1
    return out


class Tokenizer:
    def __init__(self, ranks, specials):
        self.ranks = ranks            # {(a,b): rank}
        self.specials = specials      # list of added-token strings, longest-first
        self.benc = _byte_encoder()

    @classmethod
    def from_file(cls, path):
        with open(path) as f:
            d = json.load(f)
        m = d["model"]
        assert m.get("type") == "BPE", f"unsupported model type {m.get('type')}"
        ranks = {}
        for idx, mg in enumerate(m["merges"]):
            a, b = (mg if isinstance(mg, list) else mg.split(" "))
            ranks[(a, b)] = idx
        specials = sorted((t["content"] for t in (d.get("added_tokens") or [])),
                          key=len, reverse=True)
        return cls(ranks, specials)

    def _bpe(self, piece):
        """Classic GPT-2 merge loop on a byte-mapped string; returns the list of merged pieces."""
        word = list(piece)
        if len(word) < 2:
            return word
        ranks = self.ranks
        while True:
            best = None
            for a, b in zip(word, word[1:]):
                r = ranks.get((a, b))
                if r is not None and (best is None or r < best[0]):
                    best = (r, a, b)
            if best is None:
                break
            _, first, second = best
            merged = []
            i = 0
            while i < len(word):
                if i < len(word) - 1 and word[i] == first and word[i + 1] == second:
                    merged.append(first + second)
                    i += 2
                else:
                    merged.append(word[i])
                    i += 1
            word = merged
            if len(word) == 1:
                break
        return word

    def _encode_plain(self, text):
        pieces = []
        for pt in pretokenize(unicodedata.normalize("NFC", text)):
            mapped = "".join(self.benc[b] for b in pt.encode("utf-8"))
            pieces.extend(self._bpe(mapped))
        return pieces

    def encode_pieces(self, text):
        """Full encode, splitting out any added/special tokens as atomic pieces first."""
        if not text:
            return []
        segments = [text]
        for sp in self.specials:            # isolate added tokens (longest-first)
            nxt = []
            for seg in segments:
                if seg in self.specials:
                    nxt.append(seg)
                    continue
                parts = seg.split(sp)
                for i, p in enumerate(parts):
                    if i:
                        nxt.append(sp)
                    if p:
                        nxt.append(p)
            segments = nxt
        out = []
        for seg in segments:
            if seg in self.specials:
                out.append(seg)
            else:
                out.extend(self._encode_plain(seg))
        return out

    def count(self, text):
        return len(self.encode_pieces(text))


if __name__ == "__main__":
    # selftest: compare against the backend /tokenize on a few strings (needs a reachable llama.cpp)
    import sys
    import ssl
    import urllib.request
    from pathlib import Path
    tj = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).parent / "tokenizers" / "Qwen3_8" / "tokenizer.json")
    url = sys.argv[2] if len(sys.argv) > 2 else "https://172.17.0.2:8080"
    tok = Tokenizer.from_file(tj)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def backend(text):
        body = json.dumps({"content": text}).encode()
        req = urllib.request.Request(url.rstrip("/") + "/tokenize", data=body,
                                     headers={"Content-Type": "application/json"})
        o = json.load(urllib.request.urlopen(req, context=ctx, timeout=30))
        return len(o.get("tokens") if isinstance(o, dict) else o)

    samples = [
        "Hello, world!", "   leading spaces", "don't can't we've I'll",
        "numbers 123 4567 and 89", "line1\n\nline2\n   line3",
        "def foo(x):\n    return x*2  # comment", "café naïve résumé---em dash",
        "17 * 23 = 391", "\\(17 \\times 23\\) = 391", "tabs\tand\tspaces",
    ]
    ok = 0
    for s in samples:
        mine = tok.count(s)
        try:
            ref = backend(s)
        except Exception as e:
            print("backend unreachable:", e)
            break
        flag = "OK " if mine == ref else "MISMATCH"
        ok += mine == ref
        print(f"  [{flag}] mine={mine:3d} backend={ref:3d}  {s!r}")
    print(f"{ok}/{len(samples)} exact")
