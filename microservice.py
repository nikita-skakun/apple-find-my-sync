from __future__ import annotations

import asyncio
import logging
import os
import httpx
from typing import Any

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

    state = await account.login(email, password)

    if state == LoginState.REQUIRE_2FA:  # Account requires 2FA
        # This only supports SMS methods for now
        methods = await account.get_2fa_methods()

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
                f"Push succeeded for {device_id}, status: {code}, report_id={location.hashed_adv_key_b64}"
            )
            return True
        else:
            logging.warning(
                f"Push failed for {device_id}, status: {code}, report_id={location.hashed_adv_key_b64}"
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
    print(f"Logged in as: {acc.account_name} ({acc.first_name} {acc.last_name})")

    # step 2: fetch reports!
    locations = await acc.fetch_location_history(airtag)

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


# from __future__ import annotations

# import asyncio
# import logging

# from findmy import (
#     FindMyAccessory,
#     KeyPair,
#     NearbyOfflineFindingDevice,
#     OfflineFindingScanner,
#     SeparatedOfflineFindingDevice,
# )

# logging.basicConfig(level=logging.INFO)


# def _print_nearby(device: NearbyOfflineFindingDevice) -> None:
#     print(f"NEARBY Device - {device.mac_address}")
#     print(f"  Status byte:  {device.status:x}")
#     print("  Extra data:")
#     for k, v in sorted(device.additional_data.items()):
#         print(f"    {k:20}: {v}")
#     print()


# def _print_separated(device: SeparatedOfflineFindingDevice) -> None:
#     print(f"SEPARATED Device - {device.mac_address}")
#     print(f"  Public key:   {device.adv_key_b64}")
#     print(f"  Lookup key:   {device.hashed_adv_key_b64}")
#     print(f"  Status byte:  {device.status:x}")
#     print(f"  Hint byte:    {device.hint:x}")
#     print("  Extra data:")
#     for k, v in sorted(device.additional_data.items()):
#         print(f"    {k:20}: {v}")
#     print()


# async def scan(check_key: KeyPair | FindMyAccessory | None = None) -> bool:
#     scanner = await OfflineFindingScanner.create()

#     print("Scanning for FindMy-devices...\n")

#     scan_device = None

#     async for device in scanner.scan_for(10, extend_timeout=True):
#         if isinstance(device, NearbyOfflineFindingDevice):
#             _print_nearby(device)
#         elif isinstance(device, SeparatedOfflineFindingDevice):
#             _print_separated(device)
#         else:
#             print(f"Unknown device: {device}")
#             print()
#             continue

#         if check_key and device.is_from(check_key):
#             scan_device = device

#     print()
#     if scan_device:
#         print("Device was found in scan results! :D")
#     elif check_key:
#         print("Device was not found in scan results... :c")

#     return scan_device is not None and check_key is not None


# def main():
#     dev = FindMyAccessory.from_json("airtag.json")
#     asyncio.run(scan(dev))

#     print("Current scan results were used to align the accessory.")
#     print('Updated alignment will be saved to "airtag.json".')
#     dev.to_json("airtag.json")
