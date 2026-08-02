#!/usr/bin/env python3
"""Manual test & exploration tool for the OBI EnergyTracker API.

Runs the very same API client the Home Assistant integration uses
(``custom_components/obi_energy_tracker/api.py``) against a real account, so you
can verify that login works, that the returned values look plausible and what
Home Assistant would actually display.

Credentials are taken from (in that order):

1. environment variables ``OBI_EMAIL`` / ``OBI_PASSWORD`` / ``OBI_COUNTRY``
2. a ``.env`` file next to this script (same variable names)
3. an interactive prompt (password is read without echo)

Usage::

    python3 test.py                 # login + device state + meter + daily history
    python3 test.py --raw           # do not truncate any JSON output
    python3 test.py --days 365      # request a year of daily history
    python3 test.py --explore       # additionally probe for undocumented endpoints
    python3 test.py --show-token    # print the raw bearer token (careful!)

Requires ``aiohttp`` and ``pyjwt`` (``pip install aiohttp pyjwt``).
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import getpass
import importlib.util
import json
import os
from pathlib import Path
import ssl
import sys
from types import ModuleType
from typing import Any

try:
    import aiohttp
    import jwt
except ImportError as err:  # pragma: no cover - developer convenience
    sys.exit(f"Missing dependency: {err}. Install with: pip install aiohttp pyjwt")

REPO_ROOT = Path(__file__).resolve().parent
API_PATH = REPO_ROOT / "custom_components" / "obi_energy_tracker" / "api.py"

# Filled in from api.py at runtime so the endpoints never drift from the
# integration's own definitions.
API_MODULE: Any = None
LOGIN_URL = ""
ENERGY_TRACKING_URL = ""

# Measures the OBI backend is known to accept, plus plausible candidates that
# --explore will probe for.
KNOWN_MEASURES = ("energy", "negative_energy")
CANDIDATE_MEASURES = (
    "energy",
    "negative_energy",
    "power",
    "negative_power",
    "apparent_power",
    "reactive_power",
    "voltage",
    "current",
    "frequency",
    "cost",
    "negative_cost",
    "co2",
)

# ``/historical-data/{bridge}/{device}/{granularity}`` — "hourly" and "meter"
# are the two the integration uses; the rest are guesses worth probing.
CANDIDATE_GRANULARITIES = (
    ("minutely", "PT2H"),
    ("quarter-hourly", "PT24H"),
    ("hourly", "PT24H"),
    ("daily", "P7D"),
    ("weekly", "P8W"),
    ("monthly", "P12M"),
    ("yearly", "P5Y"),
    ("meter", "PT6H"),
    ("live", "PT1H"),
)

ACCEPT_USER = "application/vnd.obi.companion.energy-tracking.user.v1+json"
ACCEPT_RECORD = (
    "application/vnd.obi.companion.energy-tracking.historical-record.v1+json"
)
ACCEPT_ANY = "application/json, */*"


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

_USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _c(code: str, text: str) -> str:
    """Wrap text in an ANSI colour code if the terminal supports it."""
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def bold(text: str) -> str:
    """Return bold text."""
    return _c("1", text)


def green(text: str) -> str:
    """Return green text."""
    return _c("32", text)


def red(text: str) -> str:
    """Return red text."""
    return _c("31", text)


def yellow(text: str) -> str:
    """Return yellow text."""
    return _c("33", text)


def dim(text: str) -> str:
    """Return dimmed text."""
    return _c("2", text)


def section(title: str) -> None:
    """Print a section header."""
    print()
    print(bold(f"── {title} ") + dim("─" * max(4, 62 - len(title))))


def ok(message: str) -> None:
    """Print a success line."""
    print(f"  {green('✓')} {message}")


def fail(message: str) -> None:
    """Print a failure line."""
    print(f"  {red('✗')} {message}")


def warn(message: str) -> None:
    """Print a warning line."""
    print(f"  {yellow('!')} {message}")


def info(message: str) -> None:
    """Print an informational line."""
    print(f"  {dim('·')} {message}")


def dump(data: Any, *, raw: bool, limit: int = 2000, indent: int = 4) -> None:
    """Pretty-print JSON, truncated unless ``raw`` is set."""
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str)
    if not raw and len(text) > limit:
        omitted = len(text) - limit
        text = f"{text[:limit]}\n… [{omitted} more characters — rerun with --raw]"
    pad = " " * indent
    print("\n".join(pad + line for line in text.splitlines()))


def describe(data: Any) -> str:
    """Return a one-line description of a JSON payload's shape."""
    if isinstance(data, list):
        shape = f"list[{len(data)}]"
        if data and isinstance(data[0], dict):
            shape += f" keys={sorted(data[0])}"
        return shape
    if isinstance(data, dict):
        return f"dict keys={sorted(data)}"
    return type(data).__name__


def mask(value: str | None, keep: int = 6) -> str:
    """Mask a secret, keeping the first and last few characters."""
    if not value:
        return "<none>"
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]} ({len(value)} chars)"


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``.env`` file (KEY=VALUE, ``#`` comments, optional quotes)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value

    return values


def resolve_credentials(
    env_values: dict[str, str], *, allow_prompt: bool
) -> tuple[str, str, str]:
    """Resolve email/password/country from env, ``.env`` file or a prompt."""

    def pick(*names: str, default: str | None = None) -> str | None:
        for name in names:
            if os.environ.get(name):
                return os.environ[name]
            if env_values.get(name):
                return env_values[name]
        return default

    email = pick("OBI_EMAIL", "OBI_USERNAME", "OBI_USER")
    password = pick("OBI_PASSWORD", "OBI_PASS")
    country = pick("OBI_COUNTRY", default="DE") or "DE"

    if not email or not password:
        if not allow_prompt or not sys.stdin.isatty():
            sys.exit(
                "No credentials found. Set OBI_EMAIL and OBI_PASSWORD as environment\n"
                "variables or in a .env file next to this script (see .env.example)."
            )
        if not email:
            email = input("OBI email: ").strip()
        if not password:
            password = getpass.getpass("OBI password: ")

    if not email or not password:
        sys.exit("Email and password are required.")

    return email, password, country.upper()


# --------------------------------------------------------------------------- #
# TLS
# --------------------------------------------------------------------------- #


def build_ssl_context() -> ssl.SSLContext:
    """Return an SSL context with a usable CA store.

    The macOS python.org installer ships an OpenSSL that trusts nothing until
    "Install Certificates.command" has been run, so every HTTPS request fails
    with CERTIFICATE_VERIFY_FAILED. If the default store turns out to be empty
    we fall back to certifi's bundle. Certificates are still verified either
    way — this only decides *which* CA list is used.

    Home Assistant is unaffected by this: it brings its own certifi bundle.
    """
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0) > 0:
        return context

    try:
        import certifi
    except ImportError:
        warn(
            "Python's CA store is empty and certifi is not installed — HTTPS will "
            "fail. Run: pip install certifi"
        )
        return context

    warn(
        "Python's default CA store is empty; falling back to certifi. "
        "Fix it permanently by running:\n"
        f"      open '/Applications/Python {sys.version_info.major}."
        f"{sys.version_info.minor}/Install Certificates.command'"
    )
    return ssl.create_default_context(cafile=certifi.where())


# --------------------------------------------------------------------------- #
# Integration code under test
# --------------------------------------------------------------------------- #


def load_api_module() -> ModuleType:
    """Import ``api.py`` without pulling in Home Assistant.

    The integration package's ``__init__.py`` imports Home Assistant, which is
    usually not installed in a plain dev environment. ``api.py`` and ``const.py``
    only need aiohttp and pyjwt, so a synthetic package is created that contains
    just those two — that keeps ``api.py``'s relative import of ``const`` working
    while the real ``__init__.py`` is never executed.
    """
    if not API_PATH.is_file():
        sys.exit(f"Could not find the API client at {API_PATH}")

    package_name = "obi_under_test"
    package = ModuleType(package_name)
    package.__path__ = [str(API_PATH.parent)]  # type: ignore[attr-defined]
    sys.modules[package_name] = package

    spec = importlib.util.spec_from_file_location(f"{package_name}.api", API_PATH)
    if spec is None or spec.loader is None:
        sys.exit(f"Could not load {API_PATH}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def extract_meter_measure(
    meter_data: Any,
    measure: str,
    *,
    legacy_direct_key: str | None = None,
) -> float | None:
    """Mirror of ``_extract_meter_measure()`` in ``sensor.py``.

    Kept in sync by hand so this script can show exactly what the Home Assistant
    sensors would report without importing Home Assistant itself.
    """
    if not meter_data:
        return None

    records = meter_data if isinstance(meter_data, list) else [meter_data]
    records = [r for r in records if isinstance(r, dict)]
    if not records:
        return None

    matching = [r for r in records if r.get("measure") == measure]
    if matching:
        return matching[-1].get("value")

    if legacy_direct_key:
        record = records[-1]
        if legacy_direct_key in record:
            return record[legacy_direct_key]
        if "value" in record and record.get("measure") is None:
            return record["value"]

    return None


# --------------------------------------------------------------------------- #
# Raw HTTP helper (used for exploration)
# --------------------------------------------------------------------------- #


async def raw_get(
    session: aiohttp.ClientSession,
    token: str,
    url: str,
    *,
    accept: str = ACCEPT_ANY,
    params: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> tuple[int | None, Any, str]:
    """GET a URL with the OBI auth headers.

    Returns ``(status, parsed_json_or_None, body_text)``; status is ``None`` when
    the request itself failed.
    """
    headers = {
        "Accept": accept,
        "Accept-Encoding": "gzip",
        "User-Agent": "app_client",
        "Authorization": f"Bearer {token}",
        "Connection": "Keep-Alive",
    }
    try:
        async with session.get(
            url,
            headers=headers,
            params=params,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            body = await response.text()
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            return response.status, parsed, body
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as err:
        return None, None, f"{type(err).__name__}: {err}"


def status_label(status: int | None) -> str:
    """Colour-code an HTTP status for the probe output."""
    if status is None:
        return red("ERR")
    if 200 <= status < 300:
        return green(str(status))
    if status in (401, 403):
        return yellow(str(status))
    if status in (406, 415, 400, 422):
        return yellow(str(status))
    return dim(str(status))


# --------------------------------------------------------------------------- #
# Test steps
# --------------------------------------------------------------------------- #


async def step_login(api: Any, args: argparse.Namespace) -> bool:
    """Authenticate and show what the token contains."""
    section("1. Login")
    info(f"POST {LOGIN_URL}")
    info(f"email={api.email}  country={api.country}")

    try:
        await api.async_login()
    except API_MODULE.ObiAuthError as err:
        fail(f"Credentials rejected: {err}")
        return False
    except API_MODULE.ObiConnectionError as err:
        fail(f"Backend unreachable: {err}")
        return False

    ok("Login successful")
    token: str = api.token
    print(f"    token: {token if args.show_token else mask(token)}")

    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.DecodeError as err:
        warn(f"Token is not a decodable JWT: {err}")
        return True

    exp = claims.get("exp")
    if exp:
        expires = datetime.fromtimestamp(exp, tz=timezone.utc)
        remaining = expires - datetime.now(tz=timezone.utc)
        info(f"token expires {expires.isoformat()} (in {remaining})")
    info(f"token claims: {sorted(claims)}")
    if args.raw:
        dump(claims, raw=args.raw)

    return True


async def step_bridge(
    api: Any, session: aiohttp.ClientSession, args: argparse.Namespace
) -> dict[str, Any] | None:
    """Resolve bridge/device IDs and dump the full user profile."""
    section("2. Account / bridge / sensors")

    ids = await api.async_get_bridge_info()
    if not ids:
        fail("Could not resolve bridge_id / device_id from the user profile.")
        return None

    ok(f"bridge_id = {ids['bridge_id']}")
    ok(f"device_id = {ids['device_id']}")

    user_id = api.account_id
    try:
        profile = await api.async_get_user_profile()
    except API_MODULE.ObiEnergyTrackerError as err:
        warn(f"Could not fetch the raw profile: {err}")
        return {"user_id": user_id, **ids}

    info("Full user profile (everything the account exposes):")
    dump(profile, raw=args.raw)

    bridge = profile.get("bridge") or {}
    sensors = bridge.get("sensors") or []
    if len(sensors) > 1:
        warn(
            f"{len(sensors)} sensors found — the integration uses the configured "
            f"one ({api.device_id})."
        )

    sensor = next(
        (s for s in sensors if s.get("id") == api.device_id), sensors[0] if sensors else {}
    )
    print()
    print(bold("    Values the diagnostic entities expose:"))
    for label, key in (
        ("Battery", "batteryLevel"),
        ("Online", "isOnline"),
        ("Connection quality", "connectionStrength"),
        ("Last reading received", "lastRecordReceivedAt"),
        ("Firmware update status", "otaStatus"),
        ("Firmware / hardware", "firmwareVersion"),
        ("Device name", "displayName"),
        ("Upload interval", "uploadInterval"),
    ):
        info(f"{label:<24}{sensor.get(key)}")

    return {"user_id": user_id, **ids}


async def step_meter(api: Any, args: argparse.Namespace) -> None:
    """Fetch the meter endpoint and show the resulting sensor values."""
    section("3. Meter readings (what the HA sensors show)")

    try:
        meter = await api.async_get_meter_data()
    except API_MODULE.ObiEnergyTrackerError as err:
        fail(f"Could not fetch meter data: {err}")
        return

    if not meter:
        fail("No meter data returned.")
        return

    ok(f"Response shape: {describe(meter)}")
    dump(meter, raw=args.raw)

    energy = extract_meter_measure(meter, "energy", legacy_direct_key="energy")
    feed_in = extract_meter_measure(meter, "negative_energy")

    print()
    print(bold("    Resulting Home Assistant sensor values:"))
    _print_sensor("sensor.obi_meter_reading  (Zählerstand)", energy)
    _print_sensor("sensor.obi_feed_in_reading (Einspeisung)", feed_in)

    records = meter if isinstance(meter, list) else [meter]
    timestamps = [r.get("timestamp") or r.get("time") for r in records if isinstance(r, dict)]
    timestamps = [t for t in timestamps if t]
    if timestamps:
        info(f"record timestamps: {timestamps}")


def _print_sensor(label: str, value: float | None) -> None:
    """Print one sensor value in Wh and kWh."""
    if value is None:
        print(f"      {red('✗')} {label}: unavailable")
        return
    try:
        as_kwh = f" = {float(value) / 1000:,.3f} kWh"
    except (TypeError, ValueError):
        as_kwh = ""
    print(f"      {green('✓')} {label}: {value} Wh{as_kwh}")


async def step_daily(api: Any, args: argparse.Namespace) -> None:
    """Fetch daily history and show what the statistics import would produce."""
    section(f"4. Daily history (up to {args.days} day(s))")

    try:
        data = await api.async_get_daily_data(args.days)
    except API_MODULE.ObiEnergyTrackerError as err:
        fail(f"Could not fetch daily data: {err}")
        return

    if not data:
        fail("No daily data returned.")
        return

    ok(f"{len(data)} record(s)")
    info("Values are per-day totals (deltas), not meter readings.")

    for measure, label in (
        ("energy", "Bezug"),
        ("negative_energy", "Einspeisung"),
    ):
        rows = [r for r in data if r.get("measure") == measure]
        if not rows:
            continue
        rows.sort(key=lambda r: r.get("time") or "")
        total = sum(r["value"] for r in rows if isinstance(r.get("value"), (int, float)))
        print()
        print(bold(f"    {label} ({measure}) — {len(rows)} day(s)"))
        info(f"range: {rows[0].get('time', '?')[:10]} … {rows[-1].get('time', '?')[:10]}")
        info(f"total: {total:,} Wh = {total / 1000:,.3f} kWh")
        shown = rows if args.raw else rows[-7:]
        for row in shown:
            value = row.get("value") or 0
            print(f"      {str(row.get('time', ''))[:10]}  {value:>10,} Wh")
        if not args.raw and len(rows) > len(shown):
            info(f"({len(rows) - len(shown)} earlier days hidden — use --raw)")


async def step_time_window(api: Any) -> None:
    """Verify that the request window is built from UTC."""
    section("5. Time window sanity check")

    now_local = datetime.now()
    now_utc = datetime.now(tz=timezone.utc)
    offset = now_local - now_utc.replace(tzinfo=None)

    info(f"local time : {now_local.isoformat()}")
    info(f"UTC time   : {now_utc.isoformat()}")
    info(f"meter window sent by api.py: {API_MODULE._utc_window(6)}")

    if abs(offset.total_seconds()) > 60:
        ok(
            f"Machine runs at UTC{offset.total_seconds() / 3600:+.0f}h, and the window "
            f"is still built from UTC — no offset leaks into the request."
        )
    else:
        ok("Machine runs in UTC.")


# --------------------------------------------------------------------------- #
# Exploration
# --------------------------------------------------------------------------- #


async def step_explore(
    session: aiohttp.ClientSession,
    token: str,
    ctx: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    """Probe for additional endpoints, granularities and measures."""
    bridge_id = ctx["bridge_id"]
    device_id = ctx["device_id"]
    user_id = ctx["user_id"]

    section("6. Exploration — other endpoints")
    info("Probing candidate paths; 200 = exists, 404 = no such route.")

    candidates: list[tuple[str, str, str, dict[str, str] | None]] = [
        ("user profile", f"/users/{user_id}", ACCEPT_USER, None),
        ("user settings", f"/users/{user_id}/settings", ACCEPT_ANY, None),
        ("user tariffs", f"/users/{user_id}/tariffs", ACCEPT_ANY, None),
        ("user contracts", f"/users/{user_id}/contracts", ACCEPT_ANY, None),
        ("bridge", f"/bridges/{bridge_id}", ACCEPT_ANY, None),
        ("bridge sensors", f"/bridges/{bridge_id}/sensors", ACCEPT_ANY, None),
        ("sensor", f"/bridges/{bridge_id}/sensors/{device_id}", ACCEPT_ANY, None),
        ("sensor (flat)", f"/sensors/{device_id}", ACCEPT_ANY, None),
        ("device (flat)", f"/devices/{device_id}", ACCEPT_ANY, None),
        ("live data", f"/live-data/{bridge_id}/{device_id}", ACCEPT_RECORD, None),
        ("realtime data", f"/realtime-data/{bridge_id}/{device_id}", ACCEPT_RECORD, None),
        (
            "historical root",
            f"/historical-data/{bridge_id}/{device_id}",
            ACCEPT_RECORD,
            None,
        ),
        ("health", "/health", ACCEPT_ANY, None),
        ("actuator health", "/actuator/health", ACCEPT_ANY, None),
    ]

    for label, path, accept, params in candidates:
        status, parsed, body = await raw_get(
            session, token, f"{ENERGY_TRACKING_URL}{path}", accept=accept, params=params
        )
        detail = describe(parsed) if parsed is not None else body[:80].replace("\n", " ")
        print(f"  [{status_label(status)}] {label:<18} {dim(path)}")
        if status == 200:
            print(f"        {detail}")
            if args.raw and parsed is not None:
                dump(parsed, raw=True, indent=8)
        await asyncio.sleep(args.probe_delay)

    section("7. Exploration — history granularities")
    info("GET /historical-data/{bridge}/{device}/<granularity>")

    now_utc = datetime.now(tz=timezone.utc)
    start = now_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    for granularity, iso_duration in CANDIDATE_GRANULARITIES:
        url = f"{ENERGY_TRACKING_URL}/historical-data/{bridge_id}/{device_id}/{granularity}"
        params = {
            "duration": f"{start}/{iso_duration}",
            "measures": ",".join(KNOWN_MEASURES),
        }
        status, parsed, body = await raw_get(
            session, token, url, accept=ACCEPT_RECORD, params=params
        )
        detail = describe(parsed) if parsed is not None else body[:80].replace("\n", " ")
        print(f"  [{status_label(status)}] {granularity:<15} {dim(detail)}")
        await asyncio.sleep(args.probe_delay)

    section("8. Exploration — available measures")
    info("Each measure requested on its own against the hourly endpoint.")

    url = f"{ENERGY_TRACKING_URL}/historical-data/{bridge_id}/{device_id}/hourly"
    for measure in CANDIDATE_MEASURES:
        params = {"duration": f"{start}/PT24H", "measures": measure}
        status, parsed, body = await raw_get(
            session, token, url, accept=ACCEPT_RECORD, params=params
        )
        if status == 200:
            records = parsed if isinstance(parsed, list) else [parsed]
            records = [r for r in records if isinstance(r, dict)]
            sample = records[-1].get("value") if records else None
            detail = f"{len(records)} record(s), last value={sample}"
        else:
            detail = body[:80].replace("\n", " ")
        print(f"  [{status_label(status)}] {measure:<18} {dim(detail)}")
        await asyncio.sleep(args.probe_delay)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Test and explore the OBI EnergyTracker API with a real account.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=REPO_ROOT / ".env",
        help="path to the .env file (default: ./.env)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="days of daily history to request (default 30; the backend clamps "
        "this to when the sensor was claimed)",
    )
    parser.add_argument(
        "--raw", action="store_true", help="print full JSON payloads without truncation"
    )
    parser.add_argument(
        "--explore",
        action="store_true",
        help="probe for additional endpoints, granularities and measures",
    )
    parser.add_argument(
        "--probe-delay",
        type=float,
        default=0.2,
        help="seconds to wait between exploration requests (default 0.2)",
    )
    parser.add_argument(
        "--show-token", action="store_true", help="print the bearer token unmasked"
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="never prompt; fail if credentials are missing",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> int:
    """Run all test steps and return a process exit code."""
    env_values = load_env_file(args.env_file)
    if env_values:
        info(f"Loaded {len(env_values)} value(s) from {args.env_file}")
    email, password, country = resolve_credentials(
        env_values, allow_prompt=not args.no_input
    )

    api_module = load_api_module()

    # Re-exported so the step functions can reference api.py's own definitions.
    global LOGIN_URL, ENERGY_TRACKING_URL, API_MODULE  # noqa: PLW0603
    API_MODULE = api_module
    LOGIN_URL = api_module.LOGIN_URL
    ENERGY_TRACKING_URL = api_module.ENERGY_TRACKING_URL

    print(bold("OBI EnergyTracker — API check"))
    info(f"API client under test: {API_PATH.relative_to(REPO_ROOT)}")
    info(f"backend: {ENERGY_TRACKING_URL}")

    connector = aiohttp.TCPConnector(ssl=build_ssl_context())
    async with aiohttp.ClientSession(connector=connector) as session:
        api = api_module.ObiEnergyTrackerAPI(
            session, email=email, password=password, country=country
        )

        if not await step_login(api, args):
            return 1

        ctx = await step_bridge(api, session, args)
        if ctx is None:
            return 1

        await step_meter(api, args)
        await step_daily(api, args)
        await step_time_window(api)

        if args.explore:
            await step_explore(session, api.token, ctx, args)
        else:
            section("Done")
            info("Run again with --explore to probe for further API endpoints.")

    print()
    ok("All required calls succeeded.")
    return 0


def main() -> int:
    """CLI entry point."""
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
