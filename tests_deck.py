"""Offline tests for gamefinder.sources.deck. Run: python tests_deck.py

The JSON bodies below were recorded from the live endpoints on 2026-10-07 (trimmed where noted).
"""

import asyncio
import logging

import aiohttp

from gamefinder.http import HttpError
from gamefinder import i18n  # noqa: E402
i18n.set_lang("ru")     # these tests check the Russian texts; English has tests of its own
from gamefinder.sources.deck import (DECK_URL, PROTON_URL, deck_fields, deck_label, deck_notes,
                                     deck_status, proton_summary)

# --- Steam: saleaction/ajaxgetdeckappcompatibilityreport?nAppID=<id>&cc=us
DECK_HADES = {"success": 1, "results": {  # 1145360, Verified with an informational note
    "appid": 1145360, "resolved_category": 3, "resolved_items": [
        {"display_type": 4, "loc_token": "#SteamDeckVerified_TestResult_DefaultControllerConfigFullyFunctional"},
        {"display_type": 4, "loc_token": "#SteamDeckVerified_TestResult_ControllerGlyphsMatchDeckDevice"},
        {"display_type": 4, "loc_token": "#SteamDeckVerified_TestResult_InterfaceTextIsLegible"},
        {"display_type": 4, "loc_token": "#SteamDeckVerified_TestResult_DefaultConfigurationIsPerformant"},
        {"display_type": 1, "loc_token": "#SteamDeckVerified_TestResult_ExternalControllersNotSupportedPrimaryPlayer"}],
    "steam_deck_blog_url": "", "search_id": None,
    "steamos_resolved_category": 2, "steamos_resolved_items": [
        {"display_type": 3, "loc_token": "#SteamOS_TestResult_GameStartupFunctional"},
        {"display_type": 1, "loc_token": "#SteamOS_TestResult_ExternalControllersNotSupportedPrimaryPlayer"}],
    "machine_resolved_category": 3, "machine_resolved_items": [
        {"display_type": 4, "loc_token": "#SteamMachine_TestResult_DefaultControllerConfigFullyFunctional"}],
    "frame_resolved_category": 3, "frame_resolved_items": [
        {"display_type": 4, "loc_token": "#SteamFrame_TestResult_InterfaceTextIsLegible"}]}}
DECK_CIV6 = {"success": 1, "results": {  # 289070, Playable (empty results without cc from a RU IP)
    "appid": 289070, "resolved_category": 2, "resolved_items": [
        {"display_type": 3, "loc_token": "#SteamDeckVerified_TestResult_ControllerGlyphsDoNotMatchDeckDevice"},
        {"display_type": 3, "loc_token": "#SteamDeckVerified_TestResult_TextInputDoesNotAutomaticallyInvokesKeyboard"},
        {"display_type": 3, "loc_token": "#SteamDeckVerified_TestResult_InterfaceTextIsNotLegible"},
        {"display_type": 4, "loc_token": "#SteamDeckVerified_TestResult_DefaultControllerConfigFullyFunctional"},
        {"display_type": 4, "loc_token": "#SteamDeckVerified_TestResult_DefaultConfigurationIsPerformant"}],
    "steam_deck_blog_url": "", "search_id": None,
    "steamos_resolved_category": 2, "steamos_resolved_items": [
        {"display_type": 3, "loc_token": "#SteamOS_TestResult_GameStartupFunctional"},
        {"display_type": 1, "loc_token": "#SteamOS_TestResult_TextInputDoesNotAutomaticallyInvokesKeyboard"}],
    "machine_resolved_category": 2, "machine_resolved_items": [],
    "frame_resolved_category": None, "frame_resolved_items": []}}
DECK_1000080 = {"success": 1, "results": {  # Playable, controller layout incomplete
    "appid": 1000080, "resolved_category": 2, "resolved_items": [
        {"display_type": 3, "loc_token": "#SteamDeckVerified_TestResult_DefaultControllerConfigNotFullyFunctional"},
        {"display_type": 3, "loc_token": "#SteamDeckVerified_TestResult_TextInputDoesNotAutomaticallyInvokesKeyboard"},
        {"display_type": 3, "loc_token": "#SteamDeckVerified_TestResult_InterfaceTextIsNotLegible"},
        {"display_type": 4, "loc_token": "#SteamDeckVerified_TestResult_ControllerGlyphsMatchDeckDevice"},
        {"display_type": 4, "loc_token": "#SteamDeckVerified_TestResult_DefaultConfigurationIsPerformant"},
        {"display_type": 1, "loc_token": "#SteamDeckVerified_TestResult_ExternalControllersNotSupportedPrimaryPlayer"}],
    "steam_deck_blog_url": "", "search_id": None, "steamos_resolved_category": 2, "steamos_resolved_items": [],
    "machine_resolved_category": 2, "machine_resolved_items": [],
    "frame_resolved_category": None, "frame_resolved_items": []}}
DECK_DESTINY2 = {"success": 1, "results": {  # 1085660, Unsupported
    "appid": 1085660, "resolved_category": 1, "resolved_items": [
        {"display_type": 2, "loc_token": "#SteamDeckVerified_TestResult_UnsupportedAntiCheatConfiguration"}],
    "steam_deck_blog_url": "", "search_id": None,
    "steamos_resolved_category": 1, "steamos_resolved_items": [
        {"display_type": 2, "loc_token": "#SteamOS_TestResult_UnsupportedAntiCheatConfiguration"}],
    "machine_resolved_category": 1, "machine_resolved_items": [
        {"display_type": 2, "loc_token": "#SteamMachine_TestResult_UnsupportedAntiCheatConfiguration"}],
    "frame_resolved_category": None, "frame_resolved_items": []}}
DECK_PUBG = {"success": 1, "results": {"appid": 578080, "resolved_category": 1, "resolved_items": [
    {"display_type": 2, "loc_token": "#SteamDeckVerified_TestResult_UnsupportedAntiCheat_Other"}]}}  # trimmed
DECK_1000010 = {"success": 1, "results": {"appid": 1000010, "resolved_category": 1, "resolved_items": [
    {"display_type": 2, "loc_token": "#SteamDeckVerified_TestResult_SteamOSDoesNotSupport"}]}}  # trimmed
DECK_UNTESTED = {"success": 1, "results": {  # 1000100, never reviewed by Valve
    "appid": 1000100, "resolved_category": 0, "resolved_items": [], "steam_deck_blog_url": "",
    "search_id": None, "steamos_resolved_category": 0, "steamos_resolved_items": [],
    "machine_resolved_category": 0, "machine_resolved_items": [],
    "frame_resolved_category": 0, "frame_resolved_items": []}}
DECK_EMPTY = {"success": 1, "results": []}  # appid 99999999, or a game delisted in the IP's region

# --- ProtonDB: api/v1/reports/summaries/<id>.json (404 + an HTML page when nobody reported)
PROTON_HADES = {"bestReportedTier": "platinum", "confidence": "strong", "score": 0.91,
                "tier": "platinum", "total": 753, "trendingTier": "platinum"}
PROTON_CIV6 = {"bestReportedTier": "platinum", "confidence": "strong", "score": 0.76,
               "tier": "gold", "total": 574, "trendingTier": "platinum"}
PROTON_DESTINY2 = {"bestReportedTier": "platinum", "confidence": "strong", "score": 0.01,
                   "tier": "borked", "total": 262, "trendingTier": "borked"}
PROTON_APEX = {"bestReportedTier": "platinum", "confidence": "strong", "score": 0.52,
               "tier": "silver", "total": 1790, "trendingTier": "bronze"}
PROTON_PENDING = {"bestReportedTier": "gold", "confidence": "inadequate", "provisionalTier": "gold",
                  "score": 0.12, "tier": "pending", "total": 1, "trendingTier": "pending"}  # 1000080


class FakeHttp:
    """Stands in for gamefinder.http.Http: answers by appid, or raises what `fail` says."""

    def __init__(self, deck=None, proton=None, fail=None):
        self.deck = deck or {}
        self.proton = proton or {}
        self.fail = fail
        self.calls = []

    async def get_json(self, url, params=None, *, headers=None, interval=None, retries=3, timeout=60):
        self.calls.append({"url": url, "params": dict(params or {}), "interval": interval,
                           "retries": retries, "timeout": timeout})
        if self.fail is not None:
            raise self.fail
        if url == DECK_URL:
            return self.deck.get(params["nAppID"], DECK_EMPTY)
        appid = int(url.rsplit("/", 1)[1].removesuffix(".json"))
        if appid not in self.proton:
            raise HttpError(404, url)
        return self.proton[appid]


DECKS = {1145360: DECK_HADES, 289070: DECK_CIV6, 1000080: DECK_1000080, 1085660: DECK_DESTINY2,
         578080: DECK_PUBG, 1000010: DECK_1000010, 1000100: DECK_UNTESTED}
PROTONS = {1145360: PROTON_HADES, 289070: PROTON_CIV6, 1085660: PROTON_DESTINY2, 1172470: PROTON_APEX,
           1000080: PROTON_PENDING}


def run(coro):
    return asyncio.run(coro)


def test_deck_request_shape():
    http = FakeHttp(DECKS)
    run(deck_status(http, 289070))
    run(deck_status(http, "1145360", cc="kz"))
    first, second = http.calls
    assert first["url"] == DECK_URL == "https://store.steampowered.com/saleaction/ajaxgetdeckappcompatibilityreport"
    assert first["params"] == {"nAppID": 289070, "cc": "us"}, "country always passed"
    assert second["params"] == {"nAppID": 1145360, "cc": "kz"}
    assert first["interval"] is None, "store host keeps http.py's shared 1.6 s budget"
    assert first["timeout"] <= 30 and first["retries"] <= 3


def test_deck_categories_and_notes():
    http = FakeHttp(DECKS)
    hades = run(deck_status(http, 1145360))
    assert hades == {"category": 3, "notes": ["external controller must be selected manually"]}

    civ = run(deck_status(http, 289070))
    assert civ["category"] == 2
    assert civ["notes"] == ["shows mouse/keyboard button icons", "on-screen keyboard must be opened manually",
                            "small text"], civ["notes"]

    playable = run(deck_status(http, 1000080))
    assert playable["category"] == 2
    assert playable["notes"][0] == "some actions need the touchscreen or a community layout"
    assert playable["notes"][-1] == "external controller must be selected manually", "info notes come last"
    assert "small text" in playable["notes"]

    assert run(deck_status(http, 1085660)) == {"category": 1, "notes": ["anti-cheat blocks Steam Deck"]}
    assert run(deck_status(http, 578080)) == {"category": 1, "notes": ["unsupported anti-cheat or online service"]}
    assert run(deck_status(http, 1000010)) == {"category": 1, "notes": ["not supported yet"]}

    # only the Steam Deck block counts, never steamos_/machine_/frame_ items
    for d in (hades, civ, playable):
        assert all("SteamOS" not in n and "startup" not in n for n in d["notes"])


def test_deck_unknown():
    http = FakeHttp(DECKS)
    assert run(deck_status(http, 1000100)) == {"category": 0, "notes": []}, "never reviewed"
    assert run(deck_status(http, 99999999)) == {"category": 0, "notes": []}, "results: [] -> unknown"


def test_deck_notes_edge_cases():
    T = "#SteamDeckVerified_TestResult_"
    items = [
        {"display_type": 1, "loc_token": T + "HDRMustBeManuallyEnabled"},          # a feature, not a problem
        {"display_type": 1, "loc_token": T + "SimultaneousInputGyroTrackpadFriendly"},
        {"display_type": 3, "loc_token": T + "NotFullyFunctionalWithoutExternalUSBGuitar"},
        {"display_type": 3, "loc_token": T + "AuxFunctionalityNotAccessible_MapEditor"},
        {"display_type": 3, "loc_token": T + "BrandNewCheckFromValve"},           # unknown caveat -> humanized
        {"display_type": 1, "loc_token": T + "SomeUnknownInfo"},                  # unknown info -> dropped
        {"display_type": 4, "loc_token": T + "SomeUnknownPass"},                  # passed -> dropped
        {"display_type": 2, "loc_token": T + "UnsupportedGraphicsPerformance"},
        {"display_type": 3, "loc_token": T + "InterfaceTextIsNotLegible"},
        {"display_type": 3, "loc_token": T + "InterfaceTextIsNotLegible"},        # duplicate
        {"display_type": 3, "loc_token": T + "NotFullyFunctionalWithoutExternalKeyboard"},
        {"display_type": 3},                                                      # no token
        "junk", None, 42,
    ]
    assert deck_notes(items) == [
        "runs poorly at any settings",
        "needs an external device: USB guitar",
        "not available: map editor",
        "brand new check from valve",
        "small text",
        "an external keyboard helps",
    ], deck_notes(items)
    assert deck_notes(None) == [] and deck_notes("x") == [] and deck_notes({}) == []


def test_deck_malformed_bodies():
    bad = [None, [], "", {}, {"success": 2}, {"success": 1}, {"success": 1, "results": "x"},
           {"success": 1, "results": None}]
    for body in bad:
        assert run(deck_status(FakeHttp({7: body}), 7)) is None, body
    odd = {"success": 1, "results": {"resolved_category": None, "resolved_items": None}}
    assert run(deck_status(FakeHttp({7: odd}), 7)) == {"category": 0, "notes": []}
    for cat in (7, -1, "3", 2.0, True):
        body = {"success": 1, "results": {"resolved_category": cat, "resolved_items": []}}
        assert run(deck_status(FakeHttp({7: body}), 7))["category"] == 0, cat


def test_proton_request_shape():
    http = FakeHttp(proton=PROTONS)
    run(proton_summary(http, 289070))
    call = http.calls[0]
    assert call["url"] == PROTON_URL.format(289070) == "https://www.protondb.com/api/v1/reports/summaries/289070.json"
    assert call["params"] == {}
    assert call["interval"] == 1.0, "www.protondb.com: one request per second"


def test_proton_summary():
    http = FakeHttp(proton=PROTONS)
    assert run(proton_summary(http, 1145360)) == {
        "tier": "platinum", "trending_tier": "platinum", "best_tier": "platinum", "provisional_tier": "",
        "confidence": "strong", "score": 0.91, "total": 753}
    civ = run(proton_summary(http, 289070))
    assert civ["tier"] == "gold" and civ["trending_tier"] == "platinum" and civ["total"] == 574
    assert run(proton_summary(http, 1085660))["tier"] == "borked"
    apex = run(proton_summary(http, 1172470))
    assert apex["tier"] == "silver" and apex["trending_tier"] == "bronze"
    pending = run(proton_summary(http, 1000080))
    assert pending["tier"] == "pending" and pending["provisional_tier"] == "gold"
    assert pending["confidence"] == "inadequate" and pending["total"] == 1
    assert run(proton_summary(http, 1000100)) is None, "404 = nobody reported it"


def test_proton_malformed_bodies():
    for body in (None, [], "", {}, {"tier": None}, {"tier": 5}, {"total": 3}):
        assert run(proton_summary(FakeHttp(proton={7: body}), 7)) is None, body
    odd = run(proton_summary(FakeHttp(proton={7: {"tier": " Gold ", "score": "x", "total": None}}), 7))
    assert odd["tier"] == "gold" and odd["score"] == 0.0 and odd["total"] == 0


def test_never_raises():
    errors = [HttpError(404, "u"), HttpError(429, "u"), HttpError(500, "u"), HttpError(403, "u"),
              asyncio.TimeoutError(), aiohttp.ClientConnectionError("down"),
              ValueError("Expecting value"), OSError("boom"), KeyError("x")]
    for exc in errors:
        http = FakeHttp(DECKS, PROTONS, fail=exc)
        assert run(deck_status(http, 1145360)) is None, exc
        assert run(proton_summary(http, 1145360)) is None, exc
    assert run(deck_status(FakeHttp(DECKS), "not a number")) is None
    assert run(proton_summary(FakeHttp(proton=PROTONS), None)) is None


def test_cancellation_propagates():
    for fn in (deck_status, proton_summary):
        try:
            run(fn(FakeHttp(fail=asyncio.CancelledError()), 1))
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError(f"{fn.__name__} swallowed CancelledError")


def test_deck_fields():
    http = FakeHttp(DECKS, PROTONS)

    def fields(appid):
        return deck_fields(run(deck_status(http, appid)), run(proton_summary(http, appid)))

    assert fields(1145360) == {"deck": 3, "deck_notes": "external controller must be selected manually",
                               "proton": "platinum", "deck_ok": 1}
    civ = fields(289070)
    assert civ["deck"] == 2 and civ["proton"] == "gold" and civ["deck_ok"] == 1
    assert civ["deck_notes"] == ("shows mouse/keyboard button icons, on-screen keyboard must be opened manually, "
                                 "small text")
    assert fields(1085660) == {"deck": 1, "deck_notes": "anti-cheat blocks Steam Deck", "proton": "borked",
                               "deck_ok": 1}
    assert fields(1000080)["proton"] == "", "pending tier is not a verdict"
    assert fields(1000100) == {"deck": 0, "deck_notes": "", "proton": "", "deck_ok": 1}, "known unknown"

    # fetch failed: same keys, deck_ok=0 so the caller can retry later
    assert deck_fields(None, None) == {"deck": 0, "deck_notes": "", "proton": "", "deck_ok": 0}
    assert deck_fields(None, {"tier": "native"})["proton"] == "native"
    assert deck_fields({"category": 9, "notes": "oops"}, {"tier": "ultra"}) == \
        {"deck": 0, "deck_notes": "", "proton": "", "deck_ok": 1}
    assert deck_fields("junk", ["junk"]) == {"deck": 0, "deck_notes": "", "proton": "", "deck_ok": 0}

    # every field is a plain SQLite-ready scalar
    for v in fields(289070).values():
        assert isinstance(v, (int, str))


def test_deck_label():
    assert deck_label(3, "") == "Steam Deck: проверено"
    assert deck_label(2, "") == "Steam Deck: играбельно"
    assert deck_label(1, "") == "Steam Deck: не поддерживается"
    assert deck_label(0, "gold") == "ProtonDB: gold"
    assert deck_label(3, "platinum") == "Steam Deck: проверено · ProtonDB: platinum"
    assert deck_label(1, "borked") == "Steam Deck: не поддерживается · ProtonDB: borked"
    assert deck_label(0, "native") == "Linux: нативная версия"
    assert deck_label(0, "") == "" and deck_label(None, None) == "" and deck_label(0, "pending") == ""
    assert deck_label(2, "GOLD") == "Steam Deck: играбельно · ProtonDB: gold"
    for deck in range(4):
        for tier in ("platinum", "gold", "silver", "bronze", "borked", "native", ""):
            assert "\n" not in deck_label(deck, tier)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} tests passed")
