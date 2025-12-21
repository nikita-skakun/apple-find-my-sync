from __future__ import annotations

import asyncio
from collections.abc import Sequence
import logging
import os
import json
import httpx
from typing import Any, cast
from datetime import datetime, timezone

from findmy import (  # pyright: ignore[reportMissingTypeStubs]
    AsyncAppleAccount,
    FindMyAccessory,
    LocalAnisetteProvider,
    LocationReport,
    LoginState,
    SmsSecondFactorMethod,
    TrustedDeviceSecondFactorMethod,
)

from findmy.accessory import FindMyAccessoryMapping  # pyright: ignore[reportMissingTypeStubs]

STORE_PATH = "account.json"
_http_client: httpx.Client = httpx.Client(http2=True, timeout=10.0)
push_url: str | None

# Exponential backoff settings (per-device, in seconds)
BASE_INTERVAL_SECONDS = 60.0  # 1 minute base interval
MAX_BACKOFF_EXPONENT = (
    5  # max exponent (effective multiplier = 2**MAX_BACKOFF_EXPONENT)
)

logging.basicConfig(level=logging.INFO)


async def _login_async(account: AsyncAppleAccount) -> None:
    email = input("email?  > ")
    password = input("passwd? > ")

    state = cast(LoginState, await account.login(email, password))  # pyright: ignore[reportUnknownMemberType]

    if state == LoginState.REQUIRE_2FA:  # Account requires 2FA
        # This only supports SMS methods for now
        methods = cast(Sequence[Any], await account.get_2fa_methods())  # pyright: ignore[reportUnknownMemberType]

        # Print the (masked) phone numbers
        for i, method in enumerate(methods):
            if isinstance(method, TrustedDeviceSecondFactorMethod):
                print(f"{i} - Trusted Device")
            elif isinstance(method, SmsSecondFactorMethod):
                print(f"{i} - SMS ({method.phone_number})")

        ind = int(input("Method? > "))

        method = methods[ind]
        await method.request()
        code = input("Code? > ")

        # This automatically finishes the post-2FA login flow
        await method.submit(code)


async def get_account_async() -> AsyncAppleAccount:
    libs_path = "ani_libs.bin"
    try:
        acc = AsyncAppleAccount.from_json(STORE_PATH, anisette_libs_path=libs_path)
    except FileNotFoundError:
        acc = AsyncAppleAccount(LocalAnisetteProvider(libs_path=libs_path))
        await _login_async(acc)

        acc.to_json(STORE_PATH)

    return acc


def _upload_location(device_id: str, location: LocationReport) -> bool:
    if not push_url:
        raise RuntimeError("Push service URL not configured")
    if not location:
        raise RuntimeError("No valid location")

    data: dict[str, Any] = {
        "id": device_id,
        "timestamp": location.timestamp.isoformat(),
        "lat": location.latitude,
        "lon": location.longitude,
        "accuracy": location.horizontal_accuracy,
        "confidence": location.confidence,
        "findmy_status": location.status,
    }

    try:
        resp = _http_client.post(push_url, data=data, timeout=10.0)
        code = getattr(resp, "status_code", None)
        return isinstance(code, int) and 200 <= code < 300
    except Exception:
        logging.exception(f"Error pushing location for {device_id}")
        return False


async def _upload_location_async(device_id: str, location: LocationReport) -> bool:
    # Run the blocking HTTP upload in a thread so we don't block the event loop.
    return await asyncio.to_thread(_upload_location, device_id, location)


async def main_sync():
    airtag_path = "airtag.json"

    # Step 0: load list of accessories
    try:
        with open(airtag_path, "r") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"Accessory file not found: {airtag_path}")

    if not isinstance(raw, list):
        raise RuntimeError(
            "Invalid format in airtag.json; expected a list of accessory mappings"
        )

    raw_list = cast(list[FindMyAccessoryMapping], raw)
    accessories: list[FindMyAccessory] = [
        FindMyAccessory.from_json(item) for item in raw_list
    ]

    # Step 1: log into an Apple account
    acc = await get_account_async()

    def _ensure_aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    pre_alignment: dict[FindMyAccessory, datetime] = {
        a: _ensure_aware(a._alignment_date)  # pyright: ignore[reportPrivateUsage]
        for a in accessories
    }

    loop = asyncio.get_running_loop()
    now = loop.time()
    backoff_state: dict[FindMyAccessory, dict[str, float | int]] = {
        a: {"exp": 0, "next_run": now} for a in accessories
    }

    def _reports_for(
        acc_obj: FindMyAccessory, locations_map: dict[Any, list[LocationReport]]
    ) -> list[LocationReport]:
        reports = locations_map.get(acc_obj)
        if reports is not None:
            return reports
        for k, v in locations_map.items():
            if k == acc_obj:
                return v
        return []

    try:
        while True:
            now = loop.time()
            # pick accessories whose schedule is due
            due = [a for a, s in backoff_state.items() if s["next_run"] <= now]
            if not due:
                # sleep until the next scheduled device
                next_run = min(s["next_run"] for s in backoff_state.values())
                sleep_for = max(0, next_run - now)
                await asyncio.sleep(sleep_for)
                continue

            try:
                locations = await acc.fetch_location_history(due)
            except Exception:
                logging.exception("Failed to fetch location history for due devices")
                # on fetch failure, increase backoff for the affected devices
                for a in due:
                    st = backoff_state[a]
                    st["exp"] = min(MAX_BACKOFF_EXPONENT, st["exp"] + 1)
                    st["next_run"] = now + BASE_INTERVAL_SECONDS * (2 ** st["exp"])
                    logging.info(
                        "Device %s fetch failed; backoff exp=%d next in %.0fs",
                        a.identifier or "<unknown>",
                        st["exp"],
                        st["next_run"] - now,
                    )
                continue

            # process each device individually
            for a in due:
                reports = _reports_for(a, locations)
                alignment_dt = pre_alignment.get(
                    a, datetime.fromtimestamp(0, tz=timezone.utc)
                )
                filtered: list[LocationReport] = []
                for loc in reports:
                    try:
                        loc_ts = _ensure_aware(loc.timestamp)
                        if loc_ts > alignment_dt:
                            filtered.append(loc)
                    except Exception:
                        logging.exception(
                            "Error while comparing location timestamp; keeping location"
                        )
                        filtered.append(loc)

                device_id = a.identifier
                if not device_id:
                    logging.warning("Accessory has no identifier set; skipping")
                    backoff_state.pop(a, None)
                    continue

                device_name = a.name
                uploaded = 0
                tried = 0

                if not filtered:
                    # no new reports: increase backoff
                    st = backoff_state[a]
                    st["exp"] = min(MAX_BACKOFF_EXPONENT, st["exp"] + 1)
                    st["next_run"] = now + BASE_INTERVAL_SECONDS * (2 ** st["exp"])
                    logging.info(
                        "Device ID: %s | Name: %s | No new reports; backoff exp=%d next in %.0fs",
                        device_id,
                        device_name,
                        st["exp"],
                        st["next_run"] - now,
                    )
                else:
                    # upload new reports in chronological order
                    filtered.sort(key=lambda r: _ensure_aware(r.timestamp))
                    for loc in filtered:
                        tried += 1
                        try:
                            if await _upload_location_async(device_id, loc):
                                uploaded += 1
                        except Exception:
                            logging.exception("Error while uploading a location")
                    logging.info(
                        "Device ID: %s | Name: %s | Uploaded: %d/%d locations",
                        device_id,
                        device_name,
                        uploaded,
                        tried,
                    )

                    # update alignment timestamp to the latest processed
                    latest_ts = max(_ensure_aware(r.timestamp) for r in filtered)
                    pre_alignment[a] = latest_ts
                    try:
                        a._alignment_date = latest_ts  # pyright: ignore[reportPrivateUsage]
                    except Exception:
                        logging.exception("Could not update accessory alignment date")

                    # reset backoff to base interval
                    st = backoff_state[a]
                    st["exp"] = 0
                    st["next_run"] = now + BASE_INTERVAL_SECONDS

                    # persist accessory alignment immediately
                    out = [x.to_json(None) for x in accessories]
                    with open(airtag_path, "w") as f:
                        json.dump(out, f, indent=4)

            # small sleep to avoid tight loop
            await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        logging.info("Service loop cancelled")
    except KeyboardInterrupt:
        logging.info("Interrupted; shutting down")
    finally:
        # ensure account is saved on shutdown
        await acc.close()
        acc.to_json(STORE_PATH)
        out = [a.to_json(None) for a in accessories]


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Apple Find My -> Traccar uploader")
    parser.add_argument(
        "--push-url",
        default=os.environ.get("PUSH_URL"),
        help="URL to which locations are uploaded",
    )

    args = parser.parse_args()
    global push_url
    push_url = args.push_url

    if not push_url:
        raise RuntimeError("Push service URL not configured")

    logging.getLogger("httpx").setLevel(logging.WARNING)

    return asyncio.run(main_sync())
