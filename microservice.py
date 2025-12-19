from __future__ import annotations

import asyncio
from collections.abc import Sequence
import logging
import os
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

STORE_PATH = "account.json"
_http_client: httpx.Client = httpx.Client(http2=True, timeout=10.0)
push_url: str | None

logging.basicConfig(level=logging.INFO)


async def _login_async(account: AsyncAppleAccount) -> None:
    email = input("email?  > ")
    password = input("passwd? > ")

    state = cast(LoginState, await account.login(email, password)) # pyright: ignore[reportUnknownMemberType]

    if state == LoginState.REQUIRE_2FA:  # Account requires 2FA
        # This only supports SMS methods for now
        methods = cast(Sequence[Any], await account.get_2fa_methods()) # pyright: ignore[reportUnknownMemberType]

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
        if code and 200 <= code < 300:
            logging.info(
                f"Push succeeded for {device_id}, status: {code}, timestamp={location.timestamp.isoformat()}"
            )
            return True
        else:
            logging.warning(
                f"Push failed for {device_id}, status: {code}, timestamp={location.timestamp.isoformat()}"
            )
            return False
    except Exception:
        logging.exception(f"Error pushing location for {device_id}")
        exit()
        return False


async def main_sync():
    airtag_path = "airtag.json"

    # Step 0: create an accessory key generator
    airtag = FindMyAccessory.from_json(airtag_path)

    # Step 1: log into an Apple account
    acc = await get_account_async()

    # step 2: fetch reports!
    locations = await acc.fetch_location_history(airtag)

    alignment_dt = airtag._alignment_date  # pyright: ignore[reportPrivateUsage]

    def _ensure_aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    alignment_dt = _ensure_aware(alignment_dt)
    before_count = len(locations)
    filtered: list[LocationReport] = []
    for loc in locations:
        try:
            loc_ts = _ensure_aware(loc.timestamp)
            if loc_ts > alignment_dt:
                filtered.append(loc)
        except Exception:
            # If anything goes wrong comparing timestamps, keep the location
            logging.exception("Error while comparing location timestamp; keeping location")
            filtered.append(loc)
    skipped = before_count - len(filtered)
    if skipped:
        logging.info(
            f"Skipping {skipped} location(s) older than alignment_date={alignment_dt.isoformat()}"
        )
    locations = filtered

    device_id = airtag.identifier
    if not device_id:
        raise RuntimeError("Accessory has no identifier set")
    device_name = airtag.name
    uploaded = 0
    tried = 0
    for loc in locations or []:
        tried += 1
        try:
            if _upload_location(device_id, loc):
                uploaded += 1
        except Exception:
            logging.exception("Error while uploading a location")
    logging.info(
        f"Device ID: {device_id} | Name: {device_name} | Uploaded: {uploaded}/{tried} locations"
    )

    await acc.close()
    acc.to_json(STORE_PATH)
    airtag.to_json(airtag_path)


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

    return asyncio.run(main_sync())
