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
from findmy.errors import EmptyResponseError, InvalidStateError  # pyright: ignore[reportMissingTypeStubs]

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
        self.airtag_path = "airtag.json"

        self.queue: asyncio.Queue[FindMyAccessory] = asyncio.Queue()
        self.processing_ids: set[str] = set()

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
            # Ensure we start with a clean state from file
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

    async def process_device_reports(
        self,
        http_client: httpx.AsyncClient,
        acc: FindMyAccessory,
        reports: list[LocationReport],
    ) -> None:
        did = acc.identifier
        if not did:
            return
        state = self.device_states.get(did)
        if not state:
            return

        filtered: list[LocationReport] = []
        for loc in reports:
            try:
                loc_aware = ensure_aware(loc.timestamp)
                if loc_aware > state.alignment_date:
                    filtered.append(loc)
            except Exception:
                logging.exception(
                    "Error while comparing location timestamp; keeping location"
                )
                filtered.append(loc)

        now = time.monotonic()
        if not filtered:
            state.apply_backoff(now)
            latest_ts = max(
                (ensure_aware(loc.timestamp) for loc in reports), default=None
            )
            logging.info(
                "Device ID: %s | Name: %s | Alignment: %s | Latest report: %s | No new reports; next in %.0fs",
                did,
                acc.name,
                state.alignment_date.isoformat(),
                latest_ts.isoformat() if latest_ts else "None",
                state.next_run - now,
            )
            return

        tasks = [self.upload_location(http_client, did, loc) for loc in filtered]
        results = await asyncio.gather(*tasks)
        uploaded = sum(results)
        tried = len(filtered)
        logging.info(
            "Device ID: %s | Name: %s | Uploaded: %d/%d locations",
            did,
            acc.name,
            uploaded,
            tried,
        )

        # Update state and persist to file
        state.alignment_date = acc._alignment_date  # pyright: ignore[reportPrivateUsage]
        state.reset_backoff(now)

        # Save to file specifically after successful processing
        self.save_accessories()

    def save_accessories(self) -> None:
        out = [x.to_json(None) for x in self.accessories]
        with open(self.airtag_path, "w") as f:
            json.dump(out, f, indent=4)

    async def worker(self, http_client: httpx.AsyncClient) -> None:
        logging.info("Worker started")
        while True:
            acc = await self.queue.get()
            did = acc.identifier
            if not did or did not in self.device_states:
                self.queue.task_done()
                if did:
                    self.processing_ids.discard(did)
                continue

            state = self.device_states[did]

            try:
                # Pre-Fetch Reset: Ensure we start from the last known good state
                # This fixes the issue where a previous failed fetch might have advanced the
                # internal cursor of the accessory object.
                acc._alignment_date = state.alignment_date  # pyright: ignore[reportPrivateUsage]

                # Fetch
                # fetch_location_history returns a dict. We only passed one accessory.
                reports_dict = await self.account.fetch_location_history([acc])  # pyright: ignore[reportOptionalMemberAccess]

                # Extract reports for this accessory
                # The key in the dict is the accessory object itself
                reports = reports_dict.get(acc, [])

                if reports:
                    await self.process_device_reports(http_client, acc, reports)
                else:
                    # Empry response logic
                    now = time.monotonic()
                    state.apply_backoff(now)
                    logging.info(
                        "Device ID: %s | Name: %s | No reports in response; next in %.0fs",
                        did,
                        acc.name,
                        state.next_run - now,
                    )

            except Exception as exc:
                # Post-Failure Reset: Rollback the internal state change that might have happened
                # inside fetch_location_history before the exception was raised.
                acc._alignment_date = state.alignment_date  # pyright: ignore[reportPrivateUsage]

                now = time.monotonic()
                state.apply_backoff(now)

                if isinstance(exc, InvalidStateError):
                    old_path = STORE_PATH
                    backup_path = STORE_PATH + ".old"
                    if os.path.exists(old_path):
                        if os.path.exists(backup_path):
                            os.remove(backup_path)
                        os.rename(old_path, backup_path)
                    logging.error(
                        "Device ID: %s | Invalid session state; moved account.json to account.json.old for re-authentication",
                        did,
                    )
                    self.account = None
                    break
                elif isinstance(
                    exc, (EmptyResponseError, OSError, asyncio.TimeoutError)
                ):
                    logging.info(
                        "Device ID: %s | Network fetch failed (%s); next in %.0fs",
                        did,
                        exc.__class__.__name__,
                        state.next_run - now,
                    )
                else:
                    logging.exception(
                        "Device ID: %s | Unexpected error during fetch", did
                    )

            finally:
                self.processing_ids.remove(did)
                self.queue.task_done()

                # Rate Limit: Ensure we don't hammer the API
                await asyncio.sleep(2.0)

    async def run(self) -> None:
        await self.load_accessories()
        self.account = await self.get_account()

        try:
            async with httpx.AsyncClient(http2=True, timeout=10.0) as http_client:
                # Start the background worker
                asyncio.create_task(self.worker(http_client))

                logging.info("Service loop started")
                while True:
                    if self.account is None:  # type: ignore[comparison]
                        logging.info("Re-authenticating due to session invalidation...")
                        self.account = await self.get_account()

                    now = time.monotonic()
                    # Check for due devices
                    for did, state in self.device_states.items():
                        if did not in self.processing_ids and state.next_run <= now:
                            acc = self.accessory_by_id.get(did)
                            if acc:
                                logging.debug("Queueing device %s", did)
                                self.processing_ids.add(did)
                                self.queue.put_nowait(acc)

                    # Sleep a bit before checking simplified schedule again
                    # We don't need accurate sleep here because the worker pulls from queue
                    await asyncio.sleep(1.0)

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

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    service = LocationSyncService(args.push_url)
    try:
        asyncio.run(service.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
