"""The frozen oracle: a reader answers the client's questions from the brief alone, and CODE decides correctness.

FROZEN (tier X): the improver never imports or calls this module and no proposal may change it. The oracle never
asks a model whether a brief is good: the reader model only answers questions, and `check_answer` (plain code)
judges each answer against keys computed from market state. It never touches the database, and it must not import
pregame.improver or pregame.gate.
"""
from __future__ import annotations

import difflib
import importlib
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping

from pregame import metrics
from pregame.contracts import (APPROVED_LANGUAGE, Brief, Context, EvalSummary, Grade, Question, QuestionResult,
                               Scenario)

MAX_WORKERS = 8                 # thread pool size for live evaluation
FAKE_READER_MIN_OVERLAP = 2     # fake reader: fewer shared content words than this -> "unknown"

# ---------------------------------------------------------------------------------------------------------------
# Text normalisation and term matching
# ---------------------------------------------------------------------------------------------------------------
_DASHES = {ord(c): "-" for c in "‐‑‒–—―⁃−﹘﹣－"}
_QUOTES = {ord("‘"): "'", ord("’"): "'", ord("‛"): "'", ord("′"): "'",
           ord("“"): '"', ord("”"): '"', ord("‟"): '"'}


def _trim_decimal(num: str) -> str:
    whole, frac = num.split(".", 1)
    frac = frac.rstrip("0")
    return f"{whole}.{frac}" if frac else whole


def normalize(text: Any) -> str:
    """Lower-case, NFKC, plain dashes/quotes, one space; numbers in one canonical form.

    "18 %", "18 percent", "18.0%" -> "18%"; "$3.85" -> "3.85"; "1,200" -> "1200"; "3.50" -> "3.5".
    """
    s = unicodedata.normalize("NFKC", "" if text is None else str(text))
    s = s.translate(_DASHES).translate(_QUOTES).lower()
    s = re.sub(r"[$€£¥]\s*(?=[-+]?\d)", " ", s)          # currency symbol before a number
    s = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", s)                       # thousands separators
    s = re.sub(r"(\d)\s*(?:percent|per cent|pct)\b", r"\1%", s)
    s = re.sub(r"(\d)\s+%", r"\1%", s)
    s = re.sub(r"\d+\.\d+", lambda m: _trim_decimal(m.group()), s)      # 18.0 -> 18, 3.50 -> 3.5
    return re.sub(r"\s+", " ", s).strip()


def _term_pattern(term_norm: str) -> re.Pattern:
    left = r"(?<!\w)" if term_norm[0].isalnum() or term_norm[0] == "_" else ""
    if term_norm[0].isdigit():
        left += r"(?<!\d\.)"                      # "5%" must not match inside "0.5%"
    last = term_norm[-1]
    if last.isalpha():
        right = r"(?:s|es)?(?!\w)"                # "strike" matches "strikes", not "striker"
    elif last.isdigit():
        right = r"(?!\w)(?!\.\d)"                 # "3.8" must not match "3.85"; "3" not "3.85"
    else:
        right = ""
    return re.compile(left + re.escape(term_norm) + right)


def contains_term(answer_norm: str, term: str) -> bool:
    """True if the (already normalised) answer contains `term` on token boundaries, after normalising the term."""
    t = normalize(term)
    return bool(t) and _term_pattern(t).search(answer_norm) is not None


# --- format variants of a KEY term (a right value written another way). Used only to find key terms: never for
# --- forbidden terms or for the numbers an "unknown" answer asserts, so these can only turn wrong-looking right
# --- answers into right ones.
_UNIT_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
               "sixteen seventeen eighteen nineteen").split()
_TENS_WORDS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_NUM_WORD_RE = re.compile(
    r"\b(?:(a|" + "|".join(_UNIT_WORDS[1:10]) + r") hundred"
    r"|(" + "|".join(_TENS_WORDS) + r")(?:[- ](" + "|".join(_UNIT_WORDS[1:10]) + r"))?"
    r"|(" + "|".join(_UNIT_WORDS) + r"))\b")
_NOT_A_NUMBER_ONE = re.compile(r"(?:\b(?:no|any|some|every|each|the|this|that|which)\s+$)")   # "no one", "the one"
_NOT_A_NUMBER_ONE_AFTER = re.compile(r"^\s+(?:of|another|day|thing|way|could|can|knows?|else)\b")


def _words_to_digits(text_norm: str) -> str:
    """"seventy-three" -> "73", "sixteen percent" -> "16%", "a hundred" -> "100" (zero..ninety-nine, N hundred)."""
    def sub(m: re.Match) -> str:
        hundreds, tens, tens_unit, unit = m.groups()
        if hundreds:
            return str(100 * (1 if hundreds == "a" else _UNIT_WORDS.index(hundreds)))
        if tens:
            return str(20 + 10 * _TENS_WORDS.index(tens) + (_UNIT_WORDS.index(tens_unit) if tens_unit else 0))
        if unit == "one" and (_NOT_A_NUMBER_ONE.search(m.string[:m.start()])
                              or _NOT_A_NUMBER_ONE_AFTER.match(m.string[m.end():])):
            return m.group()
        return str(_UNIT_WORDS.index(unit))
    return normalize(_NUM_WORD_RE.sub(sub, text_norm))


# a bare number standing for a percent key ("down 16" for "16%") must not carry some other unit
_OTHER_UNIT = (r"(?:years?|yrs?|year-olds?|days?|weeks?|wks?|months?|mos?|quarters?|hours?|hrs?|minutes?|mins?"
               r"|seconds?|k|m|mn|mm|bn|b|thousand|million|billion|trillion|dollars?|usd|cents?|bucks|bps|basis points?"
               r"|x|times|fold)")


def _bare_percent_pattern(num: str) -> re.Pattern:
    return re.compile(r"(?<![\w.])(?<!\d[-/:])" + re.escape(num) + r"(?![\w%$])(?!\.\d)(?![-/:,]\d)"
                      r"(?!\s*(?:" + _OTHER_UNIT + r")\b)(?!\s*\$)")


_SCALES = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9,
           "billion": 1e9}
_AMOUNT_RE = re.compile(r"(?<![\w.])(?<!\d[-/:])(\d+(?:\.\d+)?)(?:(k|mn|mm|m|bn|b)\b|\s*(thousand|million|billion)\b)?"
                        r"(?!\.\d)(?![\w%])")
_KEY_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)(?:(k|mn|mm|m|bn|b)|\s*(thousand|million|billion))?")
_RELATION_SCALES = {"usd_k": 1e3, "usd_m": 1e6, "usd_bn": 1e9}


def _fact_scales(question: Question) -> set[float]:
    """Money facts in thousands/millions carry the scale in the relation name ("saved_usd_k", "price_usd_m"), so a key
    "62" on a usd_k fact is the amount 62,000."""
    out = set()
    for fid in question.get("fact_ids") or []:
        parsed = _parse_fact_id(str(fid))
        relation = parsed[0][1] if parsed else ""
        out.update(scale for suffix, scale in _RELATION_SCALES.items() if relation.endswith("_" + suffix))
    return out


def _amounts(text_norm: str) -> Iterator[float]:
    for m in _AMOUNT_RE.finditer(text_norm):
        num, glued, word = m.groups()
        yield float(num) * _SCALES.get(glued or word or "", 1.0)


def _has_key(answer_norm: str, answer_alt: str, term: str, scales: set[float]) -> bool:
    """The key term, or a format variant of the same value: number words, a bare number for a percent key, or the
    same money amount written with k / thousand / M / million / commas."""
    if contains_term(answer_norm, term) or contains_term(answer_alt, term):
        return True
    t = normalize(term)
    pct = re.fullmatch(r"([-+]?\d+(?:\.\d+)?)%", t)
    if pct:
        pattern = _bare_percent_pattern(pct.group(1))
        return bool(pattern.search(answer_norm) or pattern.search(answer_alt))
    amount = _KEY_AMOUNT_RE.fullmatch(t)
    if not amount:
        return False
    base = float(amount.group(1))
    own = amount.group(2) or amount.group(3)
    targets = {base * _SCALES[own]} if own else {base} | {base * s for s in scales}
    return any(abs(v - target) <= 1e-9 * max(1.0, target)
               for v in (*_amounts(answer_norm), *_amounts(answer_alt)) for target in targets)


# --- a superseded value mentioned only as the past ("73, up from 71", "was 71, now 73", "(was 71)") is context,
# --- not the reader picking the stale number
_PAST_FILLER = (r"(?:\s+(?:about|around|roughly|approximately|approx|just|only|nearly|almost|the|an?|at|age"
                r"|its|their|your))*")
_PAST_BEFORE = re.compile(
    r"\b(?:from|was|were|had been|has been|used to be|previously|formerly|no longer|instead of|rather than|not"
    r"|replacing|replaced|replaces|old|previous|prior|former)" + _PAST_FILLER + r"\s*[(:,]?\s*$")
_PAST_AFTER = re.compile(
    r"^\s*\)?\s*,?\s*(?:before\b(?!\s+(?:tax|taxes|fees|expenses|costs|deductions|inflation)\b)|previously\b"
    r"|formerly\b|earlier\b|in the past\b|any ?more\b|prior to\b"
    r"|last (?:time|year|quarter|month|cycle|renewal|round|review|meeting)\b"
    r"|(?:a|one|two|three|\d+) (?:years?|months?|quarters?|weeks?) ago\b)")


def _only_as_past(answer_norm: str, term: str) -> bool:
    """True if `term` occurs and EVERY occurrence is framed as a past value (one bare occurrence = stated current)."""
    t = normalize(term)
    found = False
    for m in _term_pattern(t).finditer(answer_norm):
        found = True
        if not (_PAST_BEFORE.search(answer_norm[: m.start()]) or _PAST_AFTER.match(answer_norm[m.end():])):
            return False
    return found


_UNKNOWN_PATTERNS = [re.compile(p) for p in (
    r"\b(?:unknown|unknowable|unpredictable)\b",
    r"\bnot (?:known|covered|mentioned|stated|specified|included|addressed|available|provided|given|clear|sure"
    r"|certain|in (?:the|this|my|our) (?:brief|notes|prep|materials?|file)"
    r"|something (?:anyone|we|i|you|one|the brief) (?:can|could|covers?|knows?))\b",
    r"n't (?:know|cover|say|mention|state|specify|include|address|have|tell|contain|give|provide|predict|forecast"
    r"|answer|determine|be (?:known|predicted|forecast|determined|sure|certain)"
    r"|in (?:the|this|my|our) (?:brief|notes|prep|materials?|file))\b",
    r"\b(?:do|does|did) not (?:know|cover|say|mention|state|specify|include|address|have|tell|contain|give|provide)\b",
    r"\bcannot (?:say|tell|answer|confirm|determine|know|predict|forecast"
    r"|be (?:known|predicted|forecast|determined|sure|certain))\b",
    r"\bno (?:information|info|data|details?|mention|figures?|numbers?|guidance|visibility|indication|idea|clue"
    r"|record|way of knowing)\b",
    r"\b(?:nobody|no one|no-one|none of us) (?:can|could|knows?|is able to|will know)\b",
    r"\b(?:impossible|hard|too early|too soon) to (?:know|say|tell|predict|forecast|call)\b",
    r"\b(?:unable|not able) to (?:say|tell|answer|confirm|determine|know|predict|forecast)\b",
    r"\bnothing (?:in|on|about) (?:the|this|my|our|that)\b",
    r"\bsilent on\b",
    r"\bwait and see\b",
    r"\bfollow[ -]?up\b",
    r"\bneeds? (?:to )?(?:check|confirm|verify)\b",
    r"\b(?:i'll|i will|let me|we'll|we will|have to|need to) (?:check|confirm|verify|find out|look into)\b",
    r"\b(?:get|come) back to\b",
    r"\b(?:unclear|unsure|undisclosed|uncertain)\b",
    r"\boutside (?:the|this) brief\b",
    r"\bno way to (?:know|tell|predict|forecast|say)\b",
)]

_NUMBER_RE = re.compile(r"(?<![a-z0-9.])[-+]?\d+(?:\.\d+)?%?")
_YEAR_RE = re.compile(r"^(?:19|20)\d\d$")


def says_unknown(answer_norm: str) -> bool:
    return any(p.search(answer_norm) for p in _UNKNOWN_PATTERNS)


def asserted_numbers(answer_norm: str) -> list[str]:
    """Specific quantities in a normalised answer. Plain years (e.g. 2027) and labels like "q3" do not count."""
    return [n for n in _NUMBER_RE.findall(answer_norm) if not _YEAR_RE.match(n.lstrip("+-"))]


def check_answer(question: Question, answer: str) -> tuple[bool, str]:
    """Code-check one answer. Pure.

    change / balance: correct iff the answer contains ALL key_terms and NONE of forbidden_terms (key_terms may be
    empty for balance; a change question with no key terms cannot be satisfied by an "unknown"). A key term also
    matches the same value written another way (number words, "16" for "16%", "$62k" / "$62,000" for a key of 62 on a
    thousands-of-dollars fact). On a change question whose key terms are all present, a forbidden (superseded) value
    mentioned only as the past ("73, up from 71", "was 71, now 73") does not count; stated as current, it does.
    impossible: correct iff the answer says it is unknown / not in the brief / needs follow-up AND asserts no
    specific number.
    """
    kind = question.get("kind")
    text = normalize(answer)
    if not text:
        return False, "no answer"
    if kind in ("change", "balance"):
        keys = [t for t in (question.get("key_terms") or []) if normalize(t)]
        forbidden = [t for t in (question.get("forbidden_terms") or []) if normalize(t)]
        alt, scales = _words_to_digits(text), _fact_scales(question)
        missing = [t for t in keys if not _has_key(text, alt, t, scales)]
        past_ok = kind == "change" and bool(keys) and not missing
        hits = [t for t in forbidden if contains_term(text, t) and not (past_ok and _only_as_past(text, t))]
        problems = []
        if missing:
            problems.append("missing " + ", ".join(repr(t) for t in missing))
        if hits:
            problems.append("contains forbidden " + ", ".join(repr(t) for t in hits))
        if kind == "change" and not keys and says_unknown(text):
            problems.append("says unknown, so the change is not shown")
        if problems:
            return False, "; ".join(problems)
        if keys:
            return True, "has " + ", ".join(repr(t) for t in keys)
        return True, "no forbidden terms"
    if kind == "impossible":
        if not says_unknown(text):
            return False, "does not say it is unknown or needs follow-up"
        nums = asserted_numbers(text)
        if nums:
            return False, f"says unknown but asserts {nums[0]}"
        return True, "honest unknown"
    return False, f"unknown question kind {kind!r}"


# ---------------------------------------------------------------------------------------------------------------
# Fact history (supersession) and claims
# ---------------------------------------------------------------------------------------------------------------
def _utc(dt: Any) -> datetime:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_fact_id(fact_id: str) -> tuple[tuple[str, str], datetime] | None:
    """"<field>:<subject>:<relation>@<valid_from ISO date>" -> ((subject, relation), valid_from), else None."""
    if "@" not in fact_id:
        return None
    left, date = fact_id.rsplit("@", 1)
    parts = left.split(":")
    if len(parts) < 3:
        return None
    try:
        valid_from = _utc(date)
    except (ValueError, TypeError):
        return None
    return (":".join(parts[1:-1]), parts[-1]), valid_from


class _History:
    """Which facts are current as of a date: the newest per (subject, relation) with valid_from <= as_of."""

    def __init__(self, facts: list, as_of: Any):
        cutoff = _utc(as_of)
        self.known: dict[str, tuple[tuple[str, str], datetime]] = {}
        self.latest: dict[tuple[str, str], datetime] = {}
        for f in facts or []:
            key, valid_from = (f["subject"], f["relation"]), _utc(f["valid_from"])
            self.known[str(f["_id"])] = (key, valid_from)
            if valid_from <= cutoff and (key not in self.latest or valid_from > self.latest[key]):
                self.latest[key] = valid_from

    def superseded_by(self, fact_id: str) -> datetime | None:
        """The valid_from of the newer fact that replaced `fact_id`, or None if it is current (or unknown)."""
        info = self.known.get(fact_id) or _parse_fact_id(fact_id)
        if info is None:
            return None
        key, valid_from = info
        newest = self.latest.get(key)
        return newest if newest is not None and newest > valid_from else None


def _claims(brief: Brief) -> Iterator[tuple[str, str, list[str]]]:
    """(label, text, fact_ids) for every claim in every section."""
    for section, claims in (brief.get("sections") or {}).items():
        for i, claim in enumerate(claims or [], start=1):
            if isinstance(claim, Mapping):
                text, ids = str(claim.get("text", "")), claim.get("fact_ids") or []
            else:
                text, ids = str(claim), []
            yield f"{section} #{i}", text, [str(x) for x in ids]


def _short(text: str, n: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 3] + "..."


# ---------------------------------------------------------------------------------------------------------------
# Guardrail checks: each returns a list of violation strings (empty = clean)
# ---------------------------------------------------------------------------------------------------------------
def check_cite_facts(brief: Brief, ctx: Context) -> list[str]:
    """Every claim cites >= 1 fact id, and every cited id is a fact in the context."""
    in_ctx = {str(f["_id"]) for f in ctx.get("facts") or []}
    out = []
    for label, text, ids in _claims(brief):
        if not ids:
            out.append(f"{label}: claim cites no fact: \"{_short(text)}\"")
        for fid in ids:
            if fid not in in_ctx:
                out.append(f"{label}: cites {fid}, which is not in the context")
    return out


def check_no_stale_facts(brief: Brief, ctx: Context) -> list[str]:
    """No claim cites a fact that a newer (subject, relation) fact had replaced by ctx.as_of."""
    history = _History(ctx.get("facts") or [], ctx["as_of"])
    out = []
    for label, _text, ids in _claims(brief):
        for fid in ids:
            newer = history.superseded_by(fid)
            if newer is not None:
                out.append(f"{label}: cites superseded fact {fid} (replaced {newer.date().isoformat()})")
    return out


_ADVICE_PATTERNS = [(re.compile(p), label) for p, label in (
    (r"\byou (?:should|must|ought to|need to) (?:buy|sell|short|invest|dump|move into|cash out|liquidate"
     r"|annuitize|roll over)\b", "trade instruction"),
    (r"\b(?:buy|sell|dump|cash out|liquidate) (?:all |some |more )?(?:of )?your\b", "trade instruction"),
    (r"\b(?:we|i)(?: would|'d)? (?:recommend|advise|suggest) (?:that )?(?:you )?(?:buy|buying|sell|selling|short"
     r"|shorting|invest|investing)\b", "trade recommendation"),
    # ...but a third party's stated plan is news, not an instruction ("The buyer plans to buy the shares at close")
    (r"(?<!\bplans to )(?<!\bplan to )(?<!\bplanning to )(?<!\bagreed to )(?<!\bagrees to )(?<!\boffered to )"
     r"(?<!\boffers to )(?<!\bintends to )(?<!\bexpects to )(?<!\bexpected to )(?<!\bwants to )(?<!\bseeks to )"
     r"(?<!\baims to )(?<!\bproposed to )(?<!\bproposes to )"
     r"\b(?:buy|sell|short) (?:the |their |its |more |your )?(?:stocks?|shares|equit(?:y|ies)|bonds?|futures"
     r"|options|securities|funds?|annuit(?:y|ies))\b", "trade instruction"),
    # allocation advice to the client, in the ordinary ways an advisor might phrase it (Codex HDY-37)
    (r"\b(?:we|i)(?: would|'d)? (?:recommend|advise|suggest) (?:that )?(?:you |they |the client )?(?:allocat"
     r"|increas|decreas|reduc|mov|shift|rebalanc|reallocat|switch|put|add)\w*\b", "allocation recommendation"),
    (r"\byou (?:should|must|ought to|need to) (?:increase|decrease|reduce|raise|lower|shift|move|rebalance"
     r"|reallocate|allocate|put|add|switch)\b", "allocation instruction"),
    (r"\bconsider (?:moving|shifting|reallocating|allocating|rebalancing|buying|selling|adding|putting|increasing"
     r"|reducing|switching|rolling)\b", "allocation suggestion"),
    (r"\b(?:increase|decrease|reduce|raise|lower|boost|cut) (?:your|their|the client's) (?:\w+ )?(?:allocation"
     r"|exposure|holdings|position)\b", "allocation instruction"),
    (r"\brebalance (?:into|out of|toward|towards|your|their)\b", "allocation instruction"),
    # a sentence that OPENS with an allocation verb and names money or a holding is an instruction to the client
    (r"(?:^|[.!?;:]\s*)(?:allocate|move|shift|reallocate|rebalance|put|invest|transfer)\b[^.!?;]{0,60}?"
     r"(?:\bportfolio\b|\bstocks?\b|\bequit(?:y|ies)\b|\bbonds?\b|\bcash\b|\bfunds?\b|\bannuit(?:y|ies)\b"
     r"|\d+(?:\.\d+)?%)"
     # ...unless the thing being moved is a conversation, not money ("Move the annuity conversation to May")
     r"(?!\s+(?:conversation|discussion|meeting|review|call|topic|questions?|paperwork|agenda))",
     "allocation instruction"),
    # "recommend/suggest/advise" followed by an allocation or portfolio mix
    (r"\b(?:we|i)(?: would|'d)? (?:recommend|suggest|advise)\b[^.!?;]{0,40}?(?:\ballocation\b|\bportfolio\b|\bmix\b"
     r"|\bsplit\b|\b\d+/\d+\b)", "allocation recommendation"),
    # deposit insurance is a fact, not a promise ("FDIC-guaranteed up to $250,000", "guaranteed by the FDIC")
    (r"(?<!\bfdic-)(?<!\bfdic )\bguaranteed\b(?! by (?:the )?fdic\b)|\b(?:we|i) guarantee\b",
     "promises a guaranteed outcome"),
    (r"\brisk[- ]free\b", "promises a risk-free outcome"),
    (r"\b(?:can't|cannot) lose\b|\bsure (?:thing|bet)\b", "promises a sure outcome"),
    (r"\b(?:legal|investment|financial|tax) advice\b", "gives legal or investment advice"),
    (r"\byou (?:should|could|can) sue\b|\btake legal action\b|\bfile a (?:lawsuit|claim against)\b", "legal advice"),
)]
_NEGATION = re.compile(r"\b(?:not|no|never|avoid|avoiding|without|nor|refrain)\b|n't\b")


def check_no_advice(brief: Brief, ctx: Context) -> list[str]:
    """No investment or legal advice phrasing ("you should buy", "guaranteed", "legal advice", ...).

    A phrase negated within the three words before it ("this is not legal advice", "returns are not guaranteed")
    is allowed.
    """
    out = []
    for label, text, _ids in _claims(brief):
        norm = normalize(text)
        for pattern, why in _ADVICE_PATTERNS:
            for m in pattern.finditer(norm):
                before = " ".join(re.split(r"[.!?;]", norm[: m.start()])[-1].split()[-3:])
                if _NEGATION.search(before):
                    continue
                out.append(f"{label}: advice phrasing \"{m.group()}\" ({why})")
    return out


# approved-language (fix 7, compliance drift). Thresholds were picked on the world's fact texts (max ratio 0.42 to a
# full approved text) and hand-written rewordings (0.78-0.91) and legitimate look-alikes (max 0.62).
APPROVED_SIMILARITY = 0.7       # difflib ratio to a whole approved text at or above which a sentence is a rewording
APPROVED_CLAUSE_SIMILARITY = 0.85   # ...to one clause of it (a truncated disclosure: "You may get back less...")
APPROVED_WORD_COVERAGE = 0.8    # share of an approved text's content words a sentence repeats (a reordered one)...
# ...counts only with the disclosure's own meaning in the sentence (Codex: "Past performance and future results are
# shown in the appendix." shares the words of AS-01 but disclaims nothing).
_DISCLOSURE_SIGNALS = {
    "AS-01": re.compile(r"(?:\b(?:not|no|never|nor)\b|n't\b)[^.!?;]{0,40}?\b(?:indicat\w*|guarant\w*|predict\w*"
                        r"|reliable|guide)\b"),
    "AS-02": re.compile(r"\b(?:fall|falls|drop|drops|go down|goes down)\b[^.!?;]{0,30}?\b(?:rise|rises|up)\b"
                        r"|\b(?:rise|rises|go up|goes up)\b[^.!?;]{0,30}?\b(?:fall|falls|down)\b"
                        r"|\bback less\b|\bless than (?:you|they|he|she|we) (?:invested|put in)\b"),
}


def _approved_targets() -> list[tuple[str, str, list[str], set[str]]]:
    """(id, normalised text, normalised clauses, content words) per approved text."""
    out = []
    for aid, text in APPROVED_LANGUAGE.items():
        norm = normalize(text).rstrip(".")
        clauses = [c for c in re.split(r",\s*(?:and\s+)?", norm) if c]
        out.append((aid, norm, clauses if len(clauses) > 1 else [], _content_words(text)))
    return out


_PROMISE_PATTERNS = [(re.compile(p), label) for p, label in (
    (r"\bprotect(?:s|ed|ing)? (?:your |their |his |her |our |the client's |the clients' )?(?:capital|principal"
     r"|savings|money|nest egg)\b", "promises to protect capital"),
    (r"\b(?:capital|principal|savings) (?:is|are|will be|stays|remains) (?:safe|protected|secure)\b",
     "promises capital is safe"),
    (r"\bguarantee(?:s|d)? (?:(?:a|an|the|your|their|steady|positive|\S+%) )?(?:returns?|growth|income|gains?"
     r"|profits?|yields?)\b", "promises a guaranteed outcome"),
    (r"\b(?:can't|cannot|can not|won't|will not) (?:ever )?lose\b", "promises no loss"),
    (r"\brisk[- ]free\b", "promises no risk"),
    (r"\b(?:no|zero)[- ]risk\b(?!-)", "promises no risk"),
)]


def check_approved_language(brief: Brief, ctx: Context) -> list[str]:
    """No claim sentence rewords an approved disclosure, and none promises a protected or guaranteed outcome.

    Disclosures are inserted by code (drafter.render_markdown) outside the claims, so only claims are checked, and a
    claim may not carry one at all: the exact approved text in a claim is flagged (it would duplicate the inserted
    block), and so is a sentence that resembles it (APPROVED_SIMILARITY to the whole text, APPROVED_CLAUSE_SIMILARITY
    to a clause, or APPROVED_WORD_COVERAGE of its content words plus the disclosure's meaning). A promise
    negated within the three words before it ("the fund is not risk-free") is allowed, as is a question: a likely
    client question is neither a disclosure nor a promise.
    """
    targets = _approved_targets()
    out = []
    for label, text, _ids in _claims(brief):
        for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
            norm = normalize(sentence)
            if not norm or norm.endswith("?"):
                continue
            body = norm.rstrip(".!")
            words = _content_words(sentence)
            for aid, target, clauses, target_words in targets:
                if body == target:
                    out.append(f"{label}: writes approved disclosure {aid} in a claim (disclosures are inserted by "
                               "code, never drafted)")
                    break
                ratio = difflib.SequenceMatcher(None, body, target).ratio()
                clause = max((difflib.SequenceMatcher(None, body, c).ratio() for c in clauses), default=0.0)
                coverage = len(words & target_words) / len(target_words) if target_words else 0.0
                signal = _DISCLOSURE_SIGNALS.get(aid)
                if (ratio >= APPROVED_SIMILARITY or clause >= APPROVED_CLAUSE_SIMILARITY
                        or (coverage >= APPROVED_WORD_COVERAGE and signal is not None and signal.search(body))):
                    out.append(f"{label}: rewords approved disclosure {aid}: \"{_short(sentence)}\" (the approved "
                               "text is inserted by code, word for word)")
                    break
            for pattern, why in _PROMISE_PATTERNS:
                for m in pattern.finditer(norm):
                    before = " ".join(norm[: m.start()].split()[-3:])
                    if _NEGATION.search(before):
                        continue
                    out.append(f"{label}: promise \"{m.group()}\" ({why})")
    return out


GUARDRAIL_CHECKS: dict[str, Callable[[Brief, Context], list[str]]] = {
    "cite-facts": check_cite_facts,
    "no-stale-facts": check_no_stale_facts,
    "no-advice": check_no_advice,
    "approved-language": check_approved_language,
}


def run_guardrails(brief: Brief, ctx: Context) -> list[str]:
    """Run every guardrail in ctx.guardrails that is enabled; an unknown check name is itself a violation."""
    out: list[str] = []
    for g in ctx.get("guardrails") or []:
        if g.get("enabled", True) is False:
            continue
        name = str(g.get("check", ""))
        gid = g.get("id") or name
        fn = GUARDRAIL_CHECKS.get(name)
        if fn is None:
            out.append(f"{gid}: unknown check {name!r}")
            continue
        out.extend(f"{gid}: {v}" for v in fn(brief, ctx))
    return out


# ---------------------------------------------------------------------------------------------------------------
# The reader: live model or deterministic fake. It sees ONLY the brief's markdown and the questions.
# ---------------------------------------------------------------------------------------------------------------
READER_SYSTEM = (
    "You are a busy financial advisor about to meet a client household for a review. You have a prep brief and "
    "nothing else. "
    "You may ONLY use the brief text: no outside knowledge, no assumptions, no guessing. "
    "Answer each client question in one sentence, using the brief's own figures and names. "
    'If the brief does not cover a question, answer exactly "unknown". '
    'Return only JSON of the form {"answers": {"<question_id>": "<one-sentence answer>"}} with every question id.'
)


def _reader_prompt(markdown: str, questions: list[Question]) -> str:
    qs = [{"id": q["id"], "question": q["text"]} for q in questions]
    return ("BRIEF (the only source you may use):\n<<<\n" + (markdown or "") + "\n>>>\n\n"
            "CLIENT QUESTIONS:\n" + json.dumps(qs, ensure_ascii=False, indent=1) + "\n\n"
            "Answer every question id above.")


def _parse_answers(raw: Any) -> dict[str, str]:
    answers = raw.get("answers", raw) if isinstance(raw, Mapping) else {}
    if isinstance(answers, list):                      # tolerate [{"id": ..., "answer": ...}]
        answers = {str(a.get("id") or a.get("question_id")): a.get("answer", "")
                   for a in answers if isinstance(a, Mapping)}
    if not isinstance(answers, Mapping):
        return {}
    return {str(k): v if isinstance(v, str) else json.dumps(v) for k, v in answers.items()}


_STOPWORDS = frozenset("""
a about above after again against all also am an and any anything are as at be because been before being below
between both but by can could did do does doing during each few for from further get going got had has have having
he her here hers him his how i if in into is it its itself just let lets me more most much many my no nor not now of
off on once only or other our ours out over own same she should so some something still such than that the their
theirs them then there these they this those through to too under until us very was we were what when where which
while who whom why will with would yet you your yours tell know see look looking
""".split())

_CITATION_RE = re.compile(r"[\[(][^\[\]()]*@[^\[\]()]*[\])]")      # [retirement:rmd_age:start_age@2026-02-11, ...]


def _stem(word: str) -> str:
    if word[0].isdigit():
        return word
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _content_words(text: str) -> set[str]:
    words = re.findall(r"[a-z]+|\d+(?:\.\d+)?%?", normalize(text))
    return {_stem(w) for w in words if w not in _STOPWORDS and (len(w) > 1 or w.isdigit())}


def _sentences(markdown: str) -> Iterator[str]:
    """Answerable sentences of a markdown brief: no headings, list markers, emphasis or fact-id citations."""
    for raw_line in (markdown or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or not re.search(r"[A-Za-z0-9]", line):
            continue
        line = re.sub(r"^(?:[-*+>]\s+|\d+[.)]\s+)+", "", line)
        line = _CITATION_RE.sub("", line)
        line = re.sub(r"[*`]", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        for part in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", line):
            part = part.strip()
            if part:
                yield part


def fake_reader_answer(markdown: str, question_text: str) -> str:
    """Deterministic reader: the brief sentence sharing the most content words with the question, else "unknown".

    Sentences that are themselves questions (e.g. the brief's likely-questions list) are not answers and are
    skipped. Ties go to the earliest sentence.
    """
    q_words = _content_words(question_text)
    if not q_words:
        return "unknown"
    need = min(FAKE_READER_MIN_OVERLAP, len(q_words))
    best, best_score = None, 0
    for sentence in _sentences(markdown):
        if sentence.endswith("?"):
            continue
        score = len(q_words & _content_words(sentence))
        if score > best_score:
            best, best_score = sentence, score
    return best if best is not None and best_score >= need else "unknown"


def _is_fake(llm: Any) -> bool:
    return llm is None or bool(getattr(llm, "is_fake", False))


def read_answers(markdown: str, questions: list[Question], llm: Any) -> dict[str, str]:
    """question_id -> the reader's answer, using only the brief markdown. Live reader errors propagate (fail loudly)."""
    if _is_fake(llm):
        return {q["id"]: fake_reader_answer(markdown, q["text"]) for q in questions}
    raw = llm.complete_json("reader", READER_SYSTEM, _reader_prompt(markdown, questions))
    return _parse_answers(raw)


# ---------------------------------------------------------------------------------------------------------------
# Grading and evaluation
# ---------------------------------------------------------------------------------------------------------------
def grade(brief: Brief, ctx: Context, scenario: Scenario, llm: Any) -> Grade:
    """Grade one brief: reader answers from the markdown, code checks each answer, plus drift and guardrails.

    Stale claims are computed against the SCENARIO's full fact history (not just the facts the compiler kept), so a
    compiler that fails to drop a superseded fact cannot hide it.
    """
    questions = list(scenario.get("questions") or [])
    answers = read_answers(brief.get("markdown", ""), questions, llm)

    results: list[QuestionResult] = []
    missed = false_alarms = honest = 0
    for q in questions:
        answer = answers.get(q["id"])
        if answer is None:
            ok, why, answer = False, "no answer from reader", ""
        else:
            ok, why = check_answer(q, answer)
        results.append({"question_id": q["id"], "kind": q["kind"], "answer": answer, "correct": ok, "reason": why})
        if q["kind"] == "change" and not ok:
            missed += 1
        elif q["kind"] == "balance" and not ok:
            false_alarms += 1
        elif q["kind"] == "impossible" and ok:
            honest += 1

    history = _History(scenario.get("facts") or [], scenario.get("as_of") or ctx["as_of"])
    in_ctx = {str(f["_id"]) for f in ctx.get("facts") or []}
    stale = uncited = 0
    for _label, _text, ids in _claims(brief):
        if any(history.superseded_by(fid) is not None for fid in ids):
            stale += 1
        if not any(fid in in_ctx for fid in ids):
            uncited += 1

    receipt = ctx.get("receipt") or brief.get("receipt") or {}
    n = len(questions)
    return {
        "scenario_id": str(scenario.get("_id", "")),
        "split": scenario.get("split", ""),
        "accuracy": sum(r["correct"] for r in results) / n if n else 0.0,
        "missed_changes": missed,
        "false_alarms": false_alarms,
        "honest_unknowns": honest,
        "stale_claims": stale,
        "uncited_claims": uncited,
        "guardrail_violations": run_guardrails(brief, ctx),
        "context_tokens": int(receipt.get("context_tokens", 0) or 0),
        "results": results,
    }


def _one_split(scenarios: list[Scenario]) -> str:
    """The single split these scenarios share ("" for none). Mixing splits would blur the temporal held-out
    isolation (tuning months 1-3, held-out months 4-6), so it is refused, not labelled."""
    splits = sorted({str(s.get("split", "")) for s in scenarios})
    if len(splits) > 1:
        raise ValueError(f"scenarios mix splits {splits}; evaluate one split at a time")
    return splits[0] if splits else ""


def evaluate_grades(cfg: Mapping, scenarios: list[Scenario], llm: Any, k: int = 2,
                    config_label: str = "champion") -> dict[str, list[Grade]]:
    """scenario_id -> k grades (run order). For each scenario and run: compile, draft, grade.

    Extra to the INTERFACES contract: the gate needs per-run grades to cache `eval_runs` rows; `evaluate` wraps this.
    """
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    _one_split(scenarios)
    field = cfg.get("field")
    seen: set[str] = set()
    for s in scenarios:
        if s.get("field") != field:
            raise ValueError(f"scenario {s.get('_id')} is for field {s.get('field')!r}, config is for {field!r}")
        if s["_id"] in seen:
            raise ValueError(f"duplicate scenario id {s['_id']}")
        seen.add(s["_id"])

    compiler = importlib.import_module("pregame.compiler")    # imported here: written in parallel, swappable in tests
    drafter = importlib.import_module("pregame.drafter")

    def run(job: tuple[Scenario, int]) -> Grade:
        scenario, _run = job
        ctx = compiler.compile_context(cfg, scenario["account"], scenario["facts"], scenario["as_of"])
        brief = drafter.draft_brief(ctx, llm, config_label)
        return grade(brief, ctx, scenario, llm)

    jobs = [(s, r) for s in scenarios for r in range(k)]
    if _is_fake(llm) or not jobs:
        results = [run(job) for job in jobs]
    else:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(jobs))) as pool:
            results = list(pool.map(run, jobs))

    grades: dict[str, list[Grade]] = {s["_id"]: [] for s in scenarios}
    for (scenario, _run), g in zip(jobs, results):
        grades[scenario["_id"]].append(g)
    return grades


def evaluate(cfg: Mapping, scenarios: list[Scenario], llm: Any, k: int = 2,
             config_label: str = "champion") -> EvalSummary:
    """Evaluate one config on a list of scenarios (one field, one split) and summarise via metrics."""
    grades = evaluate_grades(cfg, scenarios, llm, k=k, config_label=config_label)
    return metrics.summarize(config_label, _one_split(scenarios), str(cfg.get("field", "")), k, grades)
