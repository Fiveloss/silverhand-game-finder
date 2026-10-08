"""Review statistics and the sample of reviews the analyst reads.

A store score averages everyone together. Here the numbers are split the ways that matter
for a recommendation: what people say now versus all time, what people who actually played
(10+ hours) say, and how many negatives come from players who quit within the refund window.
"""

import math
import re
import time

from .sources.steam import Steam

ENGAGED_MIN = 600       # 10 hours at the time of the review
REFUND_MIN = 120        # the refund window
MIN_TEXT = 120
MAX_TEXT = 1400

_BBCODE = re.compile(r"\[/?(h\d|b|i|u|strike|spoiler|list|olist|\*|quote|code|table|tr|td|th|hr|noparse)[^\]]*\]")
_URL_TAG = re.compile(r"\[url=[^\]]*\](.*?)\[/url\]", re.S)


def clean_text(text: str) -> str:
    text = _URL_TAG.sub(r"\1", text or "")
    text = _BBCODE.sub(" ", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text


def wilson_lower(pos: int, n: int, z: float = 1.64) -> float:
    """Lower bound of the positive share: 9/10 ranks below 900/1000."""
    if n <= 0:
        return 0.0
    p = pos / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / d


def compute_stats(summary_all: dict, recent: list[dict], now: float | None = None) -> dict:
    """summary_all: query_summary for all reviews; recent: newest reviews (any language)."""
    now = now or time.time()
    total_pos = int(summary_all.get("total_positive") or 0)
    total_neg = int(summary_all.get("total_negative") or 0)
    total = total_pos + total_neg

    def share(rows):
        return (sum(r["voted_up"] for r in rows) / len(rows)) if rows else None

    engaged = [r for r in recent if _playtime(r) >= ENGAGED_MIN]
    negatives = [r for r in recent if not r["voted_up"]]
    quick_neg = [r for r in negatives if _playtime(r) < REFUND_MIN]
    days = [(now - r["timestamp_created"]) / 86400 for r in recent if r.get("timestamp_created")]
    playtimes = sorted(_playtime(r) for r in recent if r["voted_up"])
    recent_share = share(recent)
    all_share = total_pos / total if total else None
    return {
        "total": total,
        "all_share": all_share,
        "all_lower": wilson_lower(total_pos, total),
        "score_desc": summary_all.get("review_score_desc", ""),
        "recent_n": len(recent),
        "recent_share": recent_share,
        "recent_lower": wilson_lower(sum(r["voted_up"] for r in recent), len(recent)),
        "recent_span_days": round(max(days)) if days else None,
        "engaged_n": len(engaged),
        "engaged_share": share(engaged),
        "quick_negative_share": (len(quick_neg) / len(negatives)) if negatives else None,
        "median_hours_positive": round(playtimes[len(playtimes) // 2] / 60, 1) if playtimes else None,
        "trend": (recent_share - all_share) if recent_share is not None and all_share is not None
        and len(recent) >= 30 else None,
        "updated": int(now),
    }


def quality_score(stats: dict | None) -> float:
    """0..1: how well a game is received, leaning on recent and engaged players."""
    if not stats or not stats.get("total"):
        return 0.5
    parts = [(stats["all_lower"], 1.0)]
    if stats.get("recent_n", 0) >= 20:
        parts.append((stats["recent_lower"], 1.5))
    if stats.get("engaged_n", 0) >= 15 and stats.get("engaged_share") is not None:
        parts.append((wilson_lower(round(stats["engaged_share"] * stats["engaged_n"]), stats["engaged_n"]), 1.0))
    return sum(v * w for v, w in parts) / sum(w for _, w in parts)


def _playtime(r: dict) -> int:
    a = r.get("author") or {}
    return int(a.get("playtime_at_review") or a.get("playtime_forever") or 0)


def review_weight(r: dict, now: float) -> float:
    """How much a review is worth reading: played a while, found helpful, recent, substantial."""
    hours = _playtime(r) / 60
    age_days = (now - r.get("timestamp_created", now)) / 86400
    w = 1.0
    w += min(hours, 100) / 25                       # up to +4 for 100 h
    w += math.log1p(int(r.get("votes_up") or 0)) / 2
    w += max(0.0, 1 - age_days / 365)               # newer reads better
    w *= 0.5 if hours < REFUND_MIN / 60 else 1.0
    w *= 0.6 if r.get("received_for_free") else 1.0
    w *= 0.8 if r.get("written_during_early_access") else 1.0
    return w


def pick_for_analysis(reviews: list[dict], limit: int = 60, min_negative: float = 0.35,
                      now: float | None = None) -> list[dict]:
    """The reviews the analyst reads, deduped and cleaned. Negatives are over-sampled so the
    complaints are visible even for a 95% positive game; the real ratio is passed separately."""
    now = now or time.time()
    seen, pool = set(), []
    for r in reviews:
        text = clean_text(r.get("review", ""))
        key = text[:80].lower()
        if len(text) < MIN_TEXT or key in seen:
            continue
        seen.add(key)
        pool.append({
            "text": text[:MAX_TEXT],
            "up": bool(r["voted_up"]),
            "hours": round(_playtime(r) / 60, 1),
            "date": time.strftime("%Y-%m-%d", time.gmtime(r.get("timestamp_created", now))),
            "lang": r.get("language", ""),
            "helpful": int(r.get("votes_up") or 0),
            "weight": review_weight(r, now),
        })
    pos = sorted((p for p in pool if p["up"]), key=lambda p: -p["weight"])
    neg = sorted((p for p in pool if not p["up"]), key=lambda p: -p["weight"])
    n_neg = min(len(neg), max(round(limit * min_negative), limit - len(pos)))
    chosen = neg[:n_neg] + pos[:limit - n_neg]
    chosen.sort(key=lambda p: p["date"], reverse=True)
    return chosen


async def gather(steam: Steam, appid: int) -> tuple[dict, list[dict]]:
    """(stats, sample) for one game: four requests to Steam."""
    # The first page of any query carries the all-time totals in query_summary.
    summary_all, recent = await steam.reviews(appid, filter="recent", language="all")
    _, recent_en = await steam.reviews(appid, filter="recent", language="english")
    _, recent_ru = await steam.reviews(appid, filter="recent", language="russian", per_page=60)
    _, helpful = await steam.reviews(appid, filter="all", language="english", per_page=60, day_range=365)
    stats = compute_stats(summary_all, recent)
    sample = pick_for_analysis(recent_en + recent_ru + helpful)
    return stats, sample
