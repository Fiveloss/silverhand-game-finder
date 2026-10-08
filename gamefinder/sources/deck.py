"""Handheld and Linux compatibility: Steam's Deck Verified report and ProtonDB's crowd summary.

Both endpoints are public. Steam's report hides games that are delisted in the caller's region
(from a Russian IP Cyberpunk 2077 comes back as `results: []`), so the country is always passed.
Nothing here raises: a failed fetch is None, and deck_fields turns whatever came back into columns.
"""

import logging
import re

from ..http import Http, HttpError

log = logging.getLogger(__name__)

DECK_URL = "https://store.steampowered.com/saleaction/ajaxgetdeckappcompatibilityreport"
PROTON_URL = "https://www.protondb.com/api/v1/reports/summaries/{}.json"
PROTON_INTERVAL = 1.0

DECK_UNKNOWN, DECK_UNSUPPORTED, DECK_PLAYABLE, DECK_VERIFIED = 0, 1, 2, 3
PROTON_TIERS = ("platinum", "gold", "silver", "bronze", "borked", "native")

# resolved_items[].display_type: 1 informational, 2 unsupported, 3 playable caveat, 4 passed.
# Notes are ordered by it: what blocks the game first, small print last.
_SEVERITY = {2: 0, 3: 1, 1: 2}

# '#SteamDeckVerified_TestResult_<token>' -> short note; wording follows Steam's own strings.
NOTES = {
    "UnsupportedAntiCheatConfiguration": "anti-cheat blocks Steam Deck",
    "UnsupportedAntiCheat_Other": "unsupported anti-cheat or online service",
    "UnsupportedGraphicsPerformance": "runs poorly at any settings",
    "SteamOSDoesNotSupport": "not supported yet",
    "SteamOSDoesNotSupport_OperatingSystem": "needs an unsupported OS",
    "SteamOSDoesNotSupport_Retired": "retired, no longer playable",
    "SteamOSDoesNotSupport_Software": "software, not a game",
    "SteamOSDoesNotSupport_VR": "VR game",
    "InterfaceTextIsNotLegible": "small text",
    "TextInputDoesNotAutomaticallyInvokesKeyboard": "on-screen keyboard must be opened manually",
    "DefaultControllerConfigNotFullyFunctional": "some actions need the touchscreen or a community layout",
    "ControllerGlyphsDoNotMatchDeckDevice": "shows mouse/keyboard button icons",
    "DefaultConfigurationIsNotPerformant": "graphics need manual tuning",
    "LauncherInteractionIssues": "launcher needs the touchscreen",
    "NotFullyFunctionalWithoutExternalKeyboard": "an external keyboard helps",
    "DisplayOutputNotCorrectlyScaled": "wrong scaling, set the resolution manually",
    "NativeResolutionNotSupported": "no native resolution",
    "NativeResolutionNotDefault": "native resolution must be set manually",
    "DisplayOutputHasNonblockingIssues": "minor graphics glitches",
    "AudioOutputHasNonblockingIssues": "minor audio glitches",
    "VideoPlaybackHasNonblockingIssues": "some videos may not play",
    "ResumeFromSleepNotFunctional": "glitches after sleep",
    "GameOrLauncherDoesntExitCleanly": "may not exit cleanly",
    "MultiWindowAppAutomaticallySetsFocus": "several windows, focus them manually",
    "GamepadNotEnabledByDefault": "enable the controller in game settings",
    "CloudSavesNotEnabledByDefault": "enable Steam Cloud in game settings",
    "CrossPlatformCloudSavesNotSupported": "no cross-platform saves",
    "SingleplayerGameplayRequiresActiveInternetConnection": "always online",
    "FirstTimeSetupRequiresActiveInternetConnection": "first launch needs internet",
    "DeviceCompatibilityWarningsShown": "shows a compatibility warning but runs",
    "ExternalControllersNotSupportedPrimaryPlayer": "external controller must be selected manually",
    "ExternalControllersNotSupportedLocalMultiplayer": "no external controllers in local multiplayer",
}
# Families with a variable tail: 'NotFullyFunctionalWithoutExternalUSBGuitar' -> 'USB guitar'.
PREFIX_NOTES = (
    ("NotFullyFunctionalWithoutExternal", "needs an external device: {}"),
    ("AuxFunctionalityNotAccessible_", "not available: {}"),
)
# Passed checks and pure features: never a note, whatever display_type says.
PASSED = {
    "DefaultControllerConfigFullyFunctional", "ControllerGlyphsMatchDeckDevice", "InterfaceTextIsLegible",
    "DefaultConfigurationIsPerformant", "SimultaneousInputGyroTrackpadFriendly", "HDRMustBeManuallyEnabled",
    "GameStartupFunctional",
}

DECK_RU = {DECK_VERIFIED: "проверено", DECK_PLAYABLE: "играбельно", DECK_UNSUPPORTED: "не поддерживается"}
DECK_EN = {DECK_VERIFIED: "verified", DECK_PLAYABLE: "playable", DECK_UNSUPPORTED: "unsupported"}


def _humanize(token: str) -> str:
    """'USBGuitar' -> 'USB guitar', 'MapEditor' -> 'map editor'."""
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", token)
    return " ".join(w if len(w) > 1 and w.isupper() else w.lower() for w in words)


def _note(token: str, display_type) -> str | None:
    if token in NOTES:
        return NOTES[token]
    if token in PASSED:
        return None
    for prefix, fmt in PREFIX_NOTES:
        if token.startswith(prefix) and len(token) > len(prefix):
            return fmt.format(_humanize(token[len(prefix):]))
    if display_type in (2, 3):  # a check Valve added after this table was written
        return _humanize(token) or None
    return None


def deck_notes(items) -> list[str]:
    """Problem notes from resolved_items, most serious first, without duplicates."""
    found: list[tuple[int, str]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        token = str(item.get("loc_token") or "").rsplit("_TestResult_", 1)[-1]
        kind = item.get("display_type")
        note = _note(token, kind)
        if note and all(note != n for _, n in found):
            found.append((_SEVERITY.get(kind, 3), note))
    return [n for _, n in sorted(found, key=lambda x: x[0])]


def parse_deck(data) -> dict | None:
    """The report body -> {"category", "notes"}; None when it is not a report at all."""
    if not isinstance(data, dict) or data.get("success") != 1:
        return None
    res = data.get("results")
    if res == []:  # never reviewed, unknown app, or hidden in this region
        return {"category": DECK_UNKNOWN, "notes": []}
    if not isinstance(res, dict):
        return None
    cat = res.get("resolved_category")
    cat = cat if type(cat) is int and DECK_UNKNOWN <= cat <= DECK_VERIFIED else DECK_UNKNOWN
    return {"category": cat, "notes": deck_notes(res.get("resolved_items"))}


async def deck_status(http: Http, appid: int, *, cc: str = "us") -> dict | None:
    """Steam Deck Verified: {"category": 0 unknown | 1 unsupported | 2 playable | 3 verified,
    "notes": ["small text", ...]}. None when the report could not be fetched."""
    try:
        data = await http.get_json(DECK_URL, {"nAppID": int(appid), "cc": cc or "us"},
                                   retries=1, timeout=20)
        return parse_deck(data)
    except Exception as e:  # optional enrichment; CancelledError still propagates
        log.warning("deck report for %s failed: %s", appid, e)
        return None


def parse_proton(data) -> dict | None:
    if not isinstance(data, dict) or not isinstance(data.get("tier"), str):
        return None

    def tier(key: str) -> str:
        v = data.get(key)
        return v.strip().lower() if isinstance(v, str) else ""

    try:
        score = round(float(data.get("score") or 0), 2)
        total = int(data.get("total") or 0)
    except (TypeError, ValueError):
        score, total = 0.0, 0
    return {
        "tier": tier("tier"),                       # platinum..borked, or 'pending' (too few reports)
        "trending_tier": tier("trendingTier"),      # from recent reports only
        "best_tier": tier("bestReportedTier"),
        "provisional_tier": tier("provisionalTier"),  # only while tier is 'pending'
        "confidence": tier("confidence"),           # inadequate | low | moderate | good | strong
        "score": score,
        "total": total,
    }


async def proton_summary(http: Http, appid: int) -> dict | None:
    """ProtonDB's crowd verdict (see parse_proton); None when nobody reported the game (404)
    or the summary could not be fetched."""
    try:
        data = await http.get_json(PROTON_URL.format(int(appid)), interval=PROTON_INTERVAL,
                                   retries=1, timeout=20)
        return parse_proton(data)
    except HttpError as e:
        if e.status == 404:
            log.debug("no ProtonDB reports for %s", appid)
        else:
            log.warning("ProtonDB summary for %s failed: %s", appid, e)
        return None
    except Exception as e:
        log.warning("ProtonDB summary for %s failed: %s", appid, e)
        return None


def deck_fields(deck: dict | None, proton: dict | None) -> dict:
    """Flat games-table fields. deck_ok says whether Steam's report actually arrived, so a failed
    fetch can be retried instead of being stored as 'unknown' for good."""
    d = deck if isinstance(deck, dict) else {}
    cat = d.get("category")
    cat = cat if type(cat) is int and DECK_UNKNOWN <= cat <= DECK_VERIFIED else DECK_UNKNOWN
    notes = d.get("notes") if isinstance(d.get("notes"), list) else []
    tier = str((proton if isinstance(proton, dict) else {}).get("tier") or "").lower()
    return {
        "deck": cat,
        "deck_notes": ", ".join(n for n in notes if isinstance(n, str) and n),
        "proton": tier if tier in PROTON_TIERS else "",
        "deck_ok": int(isinstance(deck, dict)),
    }


def deck_label(deck: int, proton: str) -> str:
    """One line for the game card in the player's language, '' when nothing is known."""
    from ..i18n import is_ru, tr
    names = DECK_RU if is_ru() else DECK_EN
    parts = []
    if deck in names:
        parts.append(f"Steam Deck: {names[deck]}")
    proton = (proton or "").lower()
    if proton == "native":
        parts.append(tr("Linux: native", "Linux: нативная версия"))
    elif proton in PROTON_TIERS:
        parts.append(f"ProtonDB: {proton}")
    return " · ".join(parts)
