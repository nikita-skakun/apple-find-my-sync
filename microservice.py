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

    # Step 0: load list of accessories
    try:
        with open(airtag_path, "r") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise RuntimeError(f"Accessory file not found: {airtag_path}")

    if not isinstance(raw, list):
        raise RuntimeError("Invalid format in airtag.json; expected a list of accessory mappings")

    raw_list = cast(list[FindMyAccessoryMapping], raw)
    accessories: list[FindMyAccessory] = [FindMyAccessory.from_json(item) for item in raw_list]

    # Step 1: log into an Apple account
    acc = await get_account_async()

    def _ensure_aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    # Capture the alignment time for each tracker BEFORE fetching location history.
    # The fetch may update `FindMyAccessory._alignment_date`, which would make
    # newly-fetched reports appear older than the (updated) alignment and be rejected.
    pre_alignment: dict[FindMyAccessory, datetime] = {
        a: _ensure_aware(a._alignment_date)  # pyright: ignore[reportPrivateUsage]
        for a in accessories
    }

    # step 2: fetch reports for all accessories
    try:
        locations = await acc.fetch_location_history(accessories)
    except Exception:
        logging.exception("Failed to fetch location history")
        locations = {}

    # Helper to get reports for a specific accessory, being lenient with keys
    def _reports_for(acc_obj: FindMyAccessory) -> list[LocationReport]:
        reports = locations.get(acc_obj)
        if reports is not None:
            return reports
        for k, v in locations.items():
            if k == acc_obj:
                return v
        return []

    for airtag in accessories:
        reports = _reports_for(airtag)
        # Use the pre-fetched alignment datetime captured before the fetch
        alignment_dt = pre_alignment[airtag]
        before_count = len(reports)
        filtered: list[LocationReport] = []
        for loc in reports:
            try:
                loc_ts = _ensure_aware(loc.timestamp)
                if loc_ts > alignment_dt:
                    filtered.append(loc)
            except Exception:
                logging.exception("Error while comparing location timestamp; keeping location")
                filtered.append(loc)
        skipped = before_count - len(filtered)
        if skipped:
            logging.info(
                f"Skipping {skipped} location(s) older than alignment_date={alignment_dt.isoformat()} for {airtag.name or airtag.identifier}"
            )

        device_id = airtag.identifier
        if not device_id:
            logging.warning("Accessory has no identifier set; skipping")
            continue
        device_name = airtag.name
        uploaded = 0
        tried = 0
        for loc in filtered:
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

    # Save accessories back as a list
    out = [a.to_json(None) for a in accessories]
    with open(airtag_path, "w") as f:
        json.dump(out, f, indent=4)


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
