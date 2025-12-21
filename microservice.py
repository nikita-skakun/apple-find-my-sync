from __future__ import annotations

import asyncio
from collections.abc import Sequence
import logging
import os
import json
import httpx
import time
from typing import Any, cast
from datetime import datetime, timezone

from findmy import (  # pyright: ignore[reportMissingTypeStubs]
    AsyncAppleAccount,
    FindMyAccessory,
    FindMyAccessoryMapping,
    LocalAnisetteProvider,
    LocationReport,
    LoginState,
    SmsSecondFactorMethod,
    TrustedDeviceSecondFactorMethod,
)

STORE_PATH = "account.json"
push_url: str

# Exponential backoff settings (per-device, in seconds)
BASE_INTERVAL_SECONDS = 60.0  # 1 minute base interval
MAX_BACKOFF_EXPONENT = 5  # up to 32 minutes


async def _login_async(account: AsyncAppleAccount) -> None:
    email = input("email?  > ")
    password = input("passwd? > ")

    state = cast(LoginState, await account.login(email, password))  # pyright: ignore[reportUnknownMemberType]

    if state == LoginState.REQUIRE_2FA:  # Account requires 2FA
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
    if os.path.exists(STORE_PATH):
        acc = AsyncAppleAccount.from_json(STORE_PATH, anisette_libs_path=libs_path)
    else:
        acc = AsyncAppleAccount(LocalAnisetteProvider(libs_path=libs_path))
        await _login_async(acc)
        acc.to_json(STORE_PATH)
    return acc


def ensure_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def upload_location(
    http_client: httpx.AsyncClient, device_id: str, location: LocationReport
) -> bool:
    if location.confidence > 0:
        logging.info(
            f"Found non-zero confidence {location.confidence} for device {device_id} at {location.timestamp.isoformat()}"
        )

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
        resp = await http_client.post(push_url, data=data, timeout=10.0)
        return 200 <= resp.status_code < 300
    except Exception:
        logging.exception("Error pushing location for %s", device_id)
        return False


async def main_sync():
    airtag_path = "airtag.json"

    # Step 0: load list of accessories
    if not os.path.exists(airtag_path):
        raise RuntimeError(f"Accessory file not found: {airtag_path}")

    with open(airtag_path, "r") as f:
        raw = json.load(f)

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

    now = time.monotonic()
    accessories_by_id: dict[str, FindMyAccessory] = {}
    pre_alignment: dict[str, datetime] = {}
    backoff_state: dict[str, dict[str, float | int]] = {}

    for a in accessories:
        device_id = a.identifier
        if not device_id:
            logging.warning("Accessory %s has no identifier; skipping", a.name)
            continue
        accessories_by_id[device_id] = a
        alignment = getattr(a, "_alignment_date", None)
        pre_alignment[device_id] = (
            ensure_aware(alignment)
            if alignment is not None
            else datetime.fromtimestamp(0, tz=timezone.utc)
        )
        backoff_state[device_id] = {"exp": 0, "next_run": now}

    if not backoff_state:
        logging.warning("No accessories with identifiers found; exiting")
        await acc.close()
        return

    try:
        async with httpx.AsyncClient(http2=True, timeout=10.0) as http_client:
            while True:
                now = time.monotonic()
                due_ids = [
                    did for did, s in backoff_state.items() if s["next_run"] <= now
                ]
                if not due_ids:
                    next_run = min(s["next_run"] for s in backoff_state.values())
                    await asyncio.sleep(max(0, next_run - now))
                    continue

                due_accessories = [
                    accessories_by_id[did]
                    for did in due_ids
                    if did in accessories_by_id
                ]

                try:
                    locations = await acc.fetch_location_history(due_accessories)
                except Exception:
                    logging.exception(
                        "Failed to fetch location history for due devices"
                    )
                    for did in due_ids:
                        st = backoff_state[did]
                        st["exp"] = min(MAX_BACKOFF_EXPONENT, st["exp"] + 1)
                        st["next_run"] = now + BASE_INTERVAL_SECONDS * (2 ** st["exp"])
                        logging.info(
                            "Device %s fetch failed; backoff exp=%d next in %.0fs",
                            did,
                            st["exp"],
                            st["next_run"] - now,
                        )
                    continue

                locations_by_id: dict[str, list[LocationReport]] = {}
                for k, v in locations.items():
                    if isinstance(k, FindMyAccessory):
                        kid = k.identifier
                        if kid is not None:
                            locations_by_id[kid] = v

                for did in due_ids:
                    a = accessories_by_id.get(did)
                    if a is None:
                        backoff_state.pop(did, None)
                        continue

                    reports = locations_by_id.get(did, [])
                    alignment_dt = pre_alignment.get(
                        did, datetime.fromtimestamp(0, tz=timezone.utc)
                    )
                    filtered: list[LocationReport] = []
                    for loc in reports:
                        try:
                            loc_ts = ensure_aware(loc.timestamp)
                            if loc_ts > alignment_dt:
                                filtered.append(loc)
                        except Exception:
                            logging.exception(
                                "Error while comparing location timestamp; keeping location"
                            )
                            filtered.append(loc)

                    if not filtered:
                        st = backoff_state[did]
                        st["exp"] = min(MAX_BACKOFF_EXPONENT, st["exp"] + 1)
                        st["next_run"] = now + BASE_INTERVAL_SECONDS * (2 ** st["exp"])
                        logging.info(
                            "Device ID: %s | Name: %s | No new reports; backoff exp=%d next in %.0fs",
                            did,
                            a.name,
                            st["exp"],
                            st["next_run"] - now,
                        )
                        continue

                    # upload in chronological order
                    filtered.sort(key=lambda r: ensure_aware(r.timestamp))
                    uploaded = 0
                    tried = 0
                    for loc in filtered:
                        tried += 1
                        try:
                            if await upload_location(http_client, did, loc):
                                uploaded += 1
                        except Exception:
                            logging.exception("Error while uploading a location")
                    logging.info(
                        "Device ID: %s | Name: %s | Uploaded: %d/%d locations",
                        did,
                        a.name,
                        uploaded,
                        tried,
                    )

                    pre_alignment[did] = a._alignment_date  # pyright: ignore[reportPrivateUsage]
                    st = backoff_state[did]
                    st["exp"] = 0
                    st["next_run"] = now + BASE_INTERVAL_SECONDS

                    out = [x.to_json(None) for x in accessories]
                    with open(airtag_path, "w") as f:
                        json.dump(out, f, indent=4)

                await asyncio.sleep(1)

    except asyncio.CancelledError:
        logging.info("Service loop cancelled")
    except KeyboardInterrupt:
        logging.info("Interrupted; shutting down")
    finally:
        await acc.close()
        acc.to_json(STORE_PATH)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Apple Find My -> Traccar uploader")
    parser.add_argument(
        "--push-url",
        default=os.environ.get("PUSH_URL"),
        help="URL to which locations are uploaded",
    )

    args = parser.parse_args()
    if not args.push_url:
        parser.error("Push URL must be specified via --push-url or PUSH_URL env var")

    global push_url
    push_url = args.push_url

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    return asyncio.run(main_sync())


if __name__ == "__main__":
    main()
