"""Steam and SteamSpy: the catalog, player-voted tags, store facts, reviews and libraries.

All endpoints here are public; only the library import (GetOwnedGames) needs a Web API key.
"""

import logging
import re
from datetime import datetime, timezone

from ..http import Http, HttpError

log = logging.getLogger(__name__)

STORE = "https://store.steampowered.com"
SPY = "https://steamspy.com/api.php"
API = "https://api.steampowered.com"

# Store category ids.
CAT_SINGLE, CAT_MULTI, CAT_PVP, CAT_COOP, CAT_ONLINE_COOP, CAT_ONLINE_PVP, CAT_MTX = 2, 1, 49, 9, 38, 36, 35
GENRE_EARLY_ACCESS = "70"


def parse_owners(value: str) -> int:
    """'1,000,000 .. 2,000,000' -> 1000000."""
    m = re.match(r"\s*([\d,]+)", value or "")
    return int(m.group(1).replace(",", "")) if m else 0


def parse_languages(html: str) -> tuple[bool, bool]:
    """(Russian text, Russian audio) from the store's supported_languages HTML."""
    for part in (html or "").replace("<br>", ",").split(","):
        if "Russian" in part or "русский" in part.lower():
            return True, "<strong>*</strong>" in part or "*" in part
    return False, False


def parse_year(date: str) -> int | None:
    m = re.search(r"(19|20)\d\d", date or "")
    return int(m.group(0)) if m else None


def store_fields(d: dict) -> dict:
    """The parts of an appdetails 'data' object the recommender uses."""
    cats = {c["id"] for c in d.get("categories") or []}
    genres = d.get("genres") or []
    ru_text, ru_audio = parse_languages(d.get("supported_languages", ""))
    price = d.get("price_overview") or {}
    rd = (d.get("release_date") or {}).get("date", "")
    single = CAT_SINGLE in cats
    return {
        "name": d.get("name") or "",
        "genres": [g["description"] for g in genres],
        "categories": sorted(c["description"] for c in d.get("categories") or []),
        "short_desc": re.sub(r"<[^>]+>", "", d.get("short_description") or "")[:600],
        "release_date": rd,
        "release_year": parse_year(rd),
        "price_cents": 0 if d.get("is_free") else price.get("final"),
        "currency": price.get("currency", ""),
        "discount": price.get("discount_percent", 0),
        "ru_text": int(ru_text),
        "ru_audio": int(ru_audio),
        "early_access": int(any(str(g.get("id")) == GENRE_EARLY_ACCESS for g in genres)),
        "mtx": int(CAT_MTX in cats),
        "single": int(single),
        "coop": int(bool(cats & {CAT_COOP, CAT_ONLINE_COOP})),
        "pvp": int(bool(cats & {CAT_PVP, CAT_ONLINE_PVP})),
        "online_only": int(not single and bool(cats & {CAT_MULTI, CAT_ONLINE_PVP, CAT_ONLINE_COOP})),
        "drm_notice": " ".join(x for x in (d.get("drm_notice"), d.get("ext_user_account_notice")) if x),
        "is_dlc": int(d.get("type") != "game"),
        "adult": int(bool({3, 4} & set((d.get("content_descriptors") or {}).get("ids") or []))),
        "store_ok": 1,
    }


def spy_fields(d: dict) -> dict:
    tags = d.get("tags") or {}
    return {
        "name": d.get("name") or "",
        "tags": tags if isinstance(tags, dict) else {},
        "owners": parse_owners(d.get("owners", "")),
        "positive": int(d.get("positive") or 0),
        "negative": int(d.get("negative") or 0),
    }


class Steam:
    def __init__(self, http: Http, cc: str = "kz", api_key: str = ""):
        self.http = http
        self.cc = cc
        self.api_key = api_key

    # --- catalog
    async def store_list(self, kind: str, page: int, per_page: int = 100) -> list[dict]:
        """A page of games from the store's search: kind is 'topsellers', 'top_rated' or 'popular_new'.
        (SteamSpy's request=all would be the natural source, but it trickles in for minutes.)"""
        params = {"json": 1, "start": page * per_page, "count": per_page, "category1": 998, "cc": self.cc}
        if kind == "top_rated":
            params["sort_by"] = "Reviews_DESC"
        elif kind == "popular_new":
            params["filter"] = "popularnew"
        else:
            params["filter"] = "topsellers"
        data = await self.http.get_json(f"{STORE}/search/results/", params)
        out = []
        for item in (data or {}).get("items") or []:
            m = re.search(r"/apps/(\d+)/", item.get("logo", ""))
            if m:
                out.append({"appid": int(m.group(1)), "name": item.get("name", "")})
        return out

    async def spy_details(self, appid: int) -> dict:
        return await self.http.get_json(SPY, {"request": "appdetails", "appid": appid}) or {}

    async def store_details(self, appid: int) -> dict | None:
        data = await self.http.get_json(f"{STORE}/api/appdetails",
                                        {"appids": appid, "l": "english", "cc": self.cc})
        entry = (data or {}).get(str(appid)) or {}
        if not entry.get("success"):
            return None
        return entry.get("data")

    async def search(self, term: str, limit: int = 5) -> list[dict]:
        """Store search: [{appid, name}] best match first. Works with Russian titles too."""
        try:
            data = await self.http.get_json(f"{STORE}/api/storesearch/",
                                            {"term": term, "l": "russian", "cc": self.cc}, interval=1.0)
        except HttpError as e:
            log.warning("store search failed: %s", e)
            return []
        items = [i for i in (data or {}).get("items", []) if i.get("type") == "app"]
        return [{"appid": i["id"], "name": i["name"]} for i in items[:limit]]

    # --- reviews
    async def reviews(self, appid: int, *, filter: str = "recent", language: str = "all",
                      pages: int = 1, per_page: int = 100, day_range: int | None = None) -> tuple[dict, list[dict]]:
        """(query_summary of the first page, reviews). filter: recent | updated | all (= most helpful)."""
        params = {"json": 1, "filter": filter, "language": language, "num_per_page": per_page,
                  "purchase_type": "all", "cursor": "*"}
        if day_range:
            params["day_range"] = day_range
        summary, out, seen = {}, [], set()
        for _ in range(pages):
            data = await self.http.get_json(f"{STORE}/appreviews/{appid}", params, interval=1.0)
            if not data or not data.get("success"):
                break
            if not summary:
                summary = data.get("query_summary") or {}
            batch = data.get("reviews") or []
            for r in batch:
                if r["recommendationid"] not in seen:
                    seen.add(r["recommendationid"])
                    out.append(r)
            cursor = data.get("cursor")
            if not batch or not cursor or cursor == params["cursor"]:
                break
            params["cursor"] = cursor
        return summary, out

    # --- libraries
    async def resolve_profile(self, text: str) -> str | None:
        """steamid64 from a profile link, a vanity name or a bare id."""
        text = text.strip().rstrip("/")
        m = re.search(r"(7656\d{13})", text)
        if m:
            return m.group(1)
        m = re.search(r"steamcommunity\.com/id/([^/?#]+)", text)
        vanity = m.group(1) if m else (text if re.fullmatch(r"[A-Za-z0-9_-]{2,32}", text) else None)
        if not vanity or not self.api_key:
            return None
        data = await self.http.get_json(f"{API}/ISteamUser/ResolveVanityURL/v1/",
                                        {"key": self.api_key, "vanityurl": vanity})
        resp = (data or {}).get("response") or {}
        return resp.get("steamid") if resp.get("success") == 1 else None

    async def owned_games(self, steam_id: str) -> list[dict] | None:
        """[{appid, name, playtime_min, last_played}] or None when the library is private."""
        data = await self.http.get_json(f"{API}/IPlayerService/GetOwnedGames/v1/",
                                        {"key": self.api_key, "steamid": steam_id,
                                         "include_appinfo": 1, "include_played_free_games": 1})
        resp = (data or {}).get("response") or {}
        if "games" not in resp:
            return None
        return [{"appid": g["appid"], "name": g.get("name", ""),
                 "playtime_min": g.get("playtime_forever", 0),
                 "last_played": g.get("rtime_last_played", 0)} for g in resp["games"]]


def price_entry(entry: dict | None) -> dict:
    """One appdetails?filters=price_overview entry -> {"state": "paid"|"free"|"unavailable", ...}."""
    if not entry or not entry.get("success"):
        return {"state": "unavailable"}
    data = entry.get("data")
    po = data.get("price_overview") if isinstance(data, dict) else None
    if not po:
        return {"state": "free"}
    return {"state": "paid", "final": po.get("final"), "initial": po.get("initial"),
            "discount": int(po.get("discount_percent") or 0), "currency": po.get("currency", ""),
            "final_formatted": po.get("final_formatted", ""), "initial_formatted": po.get("initial_formatted", "")}


async def region_prices(http: Http, appids: list[int], cc: str) -> dict[int, dict]:
    """Prices right now in the player's store region (the source SteamDB itself reads). Games the
    region's store does not sell come back as "unavailable"; on a network error the game is absent."""
    out: dict[int, dict] = {}
    ids = [a for a in dict.fromkeys(appids) if a and a > 0]
    for i in range(0, len(ids), 40):
        chunk = ids[i:i + 40]
        try:
            data = await http.get_json(f"{STORE}/api/appdetails", {
                "appids": ",".join(map(str, chunk)), "cc": cc, "filters": "price_overview"}, timeout=20)
        except Exception as e:
            log.warning("prices for %s failed: %s", cc, e)
            continue
        for a in chunk:
            out[a] = price_entry((data or {}).get(str(a)))
    return out


def review_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else ""
