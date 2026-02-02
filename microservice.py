from __future__ import annotations

import asyncio
from collections.abc import Sequence
import logging
import os
import json
import httpx
import time
import random
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
from findmy.errors import EmptyResponseError  # pyright: ignore[reportMissingTypeStubs]

FIBONACCI_SEQUENCE = [1, 1, 2, 3, 5, 8, 13, 21, 34]
MAX_FIB_INDEX = len(FIBONACCI_SEQUENCE) - 1
BASE_INTERVAL_SECONDS = 60.0

STORE_PATH = "account.json"


class DeviceState:
    def __init__(self, alignment_date: datetime):
        self.alignment_date = alignment_date
        self.fib_index = 0
        self.next_run = 0.0

    def apply_backoff(self, now: float) -> None:
        self.fib_index = min(MAX_FIB_INDEX, self.fib_index + 1)
        self.next_run = now + BASE_INTERVAL_SECONDS * FIBONACCI_SEQUENCE[self.fib_index]

    def reset_backoff(self, now: float) -> None:
        self.fib_index = 0
        self.next_run = now + BASE_INTERVAL_SECONDS * FIBONACCI_SEQUENCE[0]


class LocationSyncService:
    def __init__(self, push_url: str):
        self.push_url = push_url
        self.account: AsyncAppleAccount | None = None
        self.accessories: list[FindMyAccessory] = []
        self.accessory_by_id: dict[str, FindMyAccessory] = {}
        self.device_states: dict[str, DeviceState] = {}
        self.network_fib_index = 0
        self.airtag_path = "airtag.json"

    async def load_accessories(self) -> None:
        if not os.path.exists(self.airtag_path):
            raise RuntimeError(f"Accessory file not found: {self.airtag_path}")

        with open(self.airtag_path, "r") as f:
            raw = json.load(f)

        if not isinstance(raw, list):
            raise RuntimeError(
                "Invalid format in airtag.json; expected a list of accessory mappings"
            )

        raw_list = cast(list[FindMyAccessoryMapping], raw)
        self.accessories = [FindMyAccessory.from_json(item) for item in raw_list]
        self.accessory_by_id = {
            a.identifier: a for a in self.accessories if a.identifier
        }

        now = time.monotonic()
        for i, a in enumerate(self.accessories):
            device_id = a.identifier
            if not device_id:
                logging.warning("Accessory %s has no identifier; skipping", a.name)
                continue
            self.device_states[device_id] = DeviceState(ensure_aware(a._alignment_date))  # pyright: ignore[reportPrivateUsage]
            self.device_states[device_id].next_run = now + (i * 10)

        if not self.device_states:
            raise RuntimeError("No accessories with identifiers found")

    async def get_account(self) -> AsyncAppleAccount:
        libs_path = "ani_libs.bin"
        if os.path.exists(STORE_PATH):
            acc = AsyncAppleAccount.from_json(STORE_PATH, anisette_libs_path=libs_path)
        else:
            acc = AsyncAppleAccount(LocalAnisetteProvider(libs_path=libs_path))
            await _login_async(acc)
            acc.to_json(STORE_PATH)
        return acc

    async def fetch_locations(
        self, due_accessories: list[FindMyAccessory]
    ) -> dict[str, list[LocationReport]]:
        try:
            locations = await self.account.fetch_location_history(due_accessories)  # pyright: ignore[reportOptionalMemberAccess]
            self.network_fib_index = 0
        except Exception as exc:
            logging.exception("Failed to fetch location history for due devices")
            if isinstance(exc, (EmptyResponseError, OSError, asyncio.TimeoutError)):
                self.network_fib_index = min(MAX_FIB_INDEX, self.network_fib_index + 1)
                delay = BASE_INTERVAL_SECONDS * FIBONACCI_SEQUENCE[
                    self.network_fib_index
                ] + random.uniform(0, 5)
                logging.info(
                    "Network fetch failed; next retry in %.0fs",
                    delay,
                )
                await asyncio.sleep(delay)
                return {}
            else:
                for a in due_accessories:
                    did = a.identifier
                    if did and did in self.device_states:
                        now = time.monotonic()
                        self.device_states[did].apply_backoff(now)
                        logging.info(
                            "Device %s fetch failed; next in %.0fs",
                            did,
                            self.device_states[did].next_run - now,
                        )
                return {}

        locations_by_id: dict[str, list[LocationReport]] = {}
        for k, v in locations.items():
            if isinstance(k, FindMyAccessory):
                kid = k.identifier
                if kid is not None:
                    locations_by_id[kid] = v
        return locations_by_id

    async def upload_location(
        self, http_client: httpx.AsyncClient, device_id: str, location: LocationReport
    ) -> bool:
        confidence_str = str(location.confidence)
        if not all(c in "01" for c in confidence_str):
            logging.error(
                f"Confidence value {location.confidence} contains non-binary digits, skipping upload"
            )
            return False

        confidence_int = int(confidence_str, 2)
        if confidence_int not in (0, 1, 2, 3):
            logging.warning(
                f"Parsed confidence {confidence_int} is not in 0-3 range for device {device_id} at {location.timestamp.isoformat()}"
            )

        data: dict[str, Any] = {
            "id": device_id,
            "timestamp": location.timestamp.isoformat(),
            "lat": location.latitude,
            "lon": location.longitude,
            "accuracy": location.horizontal_accuracy,
            "confidence": confidence_int,
            "findmy_status": location.status,
        }

        try:
            resp = await http_client.post(self.push_url, data=data, timeout=10.0)
            return 200 <= resp.status_code < 300
        except Exception:
            logging.exception("Error pushing location for %s", device_id)
            return False

    async def process_locations(
        self,
        http_client: httpx.AsyncClient,
        locations_by_id: dict[str, list[LocationReport]],
    ) -> None:
        devices_to_remove: list[str] = []
        for did, state in self.device_states.items():
            a = self.accessory_by_id.get(did)
            if a is None:
                devices_to_remove.append(did)
                continue

            reports = locations_by_id.get(did, [])
            filtered: list[LocationReport] = []
            for loc in reports:
                try:
                    if ensure_aware(loc.timestamp) > state.alignment_date:
                        filtered.append(loc)  # type: ignore[reportUnknownMemberType]
                except Exception:
                    logging.exception(
                        "Error while comparing location timestamp; keeping location"
                    )
                    filtered.append(loc)  # type: ignore[reportUnknownMemberType]

            if not filtered:
                now = time.monotonic()
                state.apply_backoff(now)
                logging.info(
                    "Device ID: %s | Name: %s | No new reports; next in %.0fs",
                    did,
                    a.name,
                    state.next_run - now,
                )
                continue

            tasks = [self.upload_location(http_client, did, loc) for loc in filtered]  # type: ignore[reportUnknownArgument]
            results = await asyncio.gather(*tasks)
            uploaded = sum(results)
            tried = len(filtered)  # type: ignore[reportUnknownArgument]
            logging.info(
                "Device ID: %s | Name: %s | Uploaded: %d/%d locations",
                did,
                a.name,
                uploaded,
                tried,
            )

            state.alignment_date = a._alignment_date  # pyright: ignore[reportPrivateUsage]
            now = time.monotonic()
            state.reset_backoff(now)

        for did in devices_to_remove:
            del self.device_states[did]

        out = [x.to_json(None) for x in self.accessories]
        with open(self.airtag_path, "w") as f:
            json.dump(out, f, indent=4)

    async def run(self) -> None:
        await self.load_accessories()
        self.account = await self.get_account()

        try:
            async with httpx.AsyncClient(http2=True, timeout=10.0) as http_client:
                while True:
                    now = time.monotonic()
                    due_ids = [
                        did
                        for did, s in self.device_states.items()
                        if s.next_run <= now
                    ]
                    if not due_ids:
                        next_run = min(s.next_run for s in self.device_states.values())
                        await asyncio.sleep(max(0, next_run - now))
                        continue

                    due_accessories = [
                        a for a in self.accessories if a.identifier in due_ids
                    ]
                    locations_by_id = await self.fetch_locations(due_accessories)
                    await self.process_locations(http_client, locations_by_id)

        except asyncio.CancelledError:
            logging.info("Service loop cancelled")
        except KeyboardInterrupt:
            logging.info("Interrupted; shutting down")
        finally:
            if self.account:
                await self.account.close()
                self.account.to_json(STORE_PATH)


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


def ensure_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


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

    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    service = LocationSyncService(args.push_url)
    return asyncio.run(service.run())


if __name__ == "__main__":
    main()
