from __future__ import annotations

from collections.abc import Awaitable, Sequence
from datetime import datetime, timezone
from typing import cast
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
import asyncio
import httpx
import inspect
import json
import logging
import os
import time

FIBONACCI_SEQUENCE = [1, 1, 2, 3, 5, 8, 13, 21]
MAX_FIB_INDEX = len(FIBONACCI_SEQUENCE) - 1
BASE_INTERVAL_SECONDS = 60.0
GLOBAL_SEND_INTERVAL_SECONDS = 2.0

APP_CONFIG_DIR = "apple-find-my-sync"
USER_ACCOUNT_FILE = "account.json"
USER_DEVICE_FILE = "devices.json"
SendRequest = tuple[
    int,
    str,
    dict[str, object],
    dict[str, str],
    asyncio.Future[bool],
]


def get_config_dir(create: bool = False) -> str:
    base_dir = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    config_dir = os.path.join(base_dir, APP_CONFIG_DIR)
    if create:
        os.makedirs(config_dir, exist_ok=True)
    return config_dir


def discover_user_dirs(root: str | None = None) -> list[tuple[int, str, str]]:
    if root is None:
        root = get_config_dir()
    users: list[tuple[int, str, str]] = []
    for entry in sorted(os.listdir(root)):
        if not entry.isdigit():
            continue
        folder = os.path.join(root, entry)
        if not os.path.isdir(folder):
            continue

        user_id = int(entry)
        account_path = os.path.join(folder, USER_ACCOUNT_FILE)
        device_path = os.path.join(folder, USER_DEVICE_FILE)

        if os.path.exists(account_path) and os.path.exists(device_path):
            users.append((user_id, account_path, device_path))

    if users:
        return users

    raise RuntimeError(
        "No user directories found. Create numeric folders containing account.json and devices.json."
    )


def load_config(path: str | None = None) -> tuple[str, str, str]:
    if path is None:
        path = os.path.join(get_config_dir(), "config.json")
    if not os.path.exists(path):
        raise RuntimeError(f"Config file not found: {path}")

    with open(path, "r") as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        raise RuntimeError("Invalid format in config.json; expected an object")

    config = cast(dict[str, object], raw)
    traccar_push_url = config.get("traccar_push_url")
    traccar_admin_api_url = config.get("traccar_admin_api_url")
    traccar_admin_api_token = config.get("traccar_admin_api_token")

    if not isinstance(traccar_push_url, str) or not traccar_push_url.strip():
        raise RuntimeError(
            "config.json must contain non-empty string: traccar_push_url"
        )
    if not isinstance(traccar_admin_api_url, str) or not traccar_admin_api_url.strip():
        raise RuntimeError(
            "config.json must contain non-empty string: traccar_admin_api_url"
        )
    if (
        not isinstance(traccar_admin_api_token, str)
        or not traccar_admin_api_token.strip()
    ):
        raise RuntimeError(
            "config.json must contain non-empty string: traccar_admin_api_token"
        )

    return traccar_push_url, traccar_admin_api_url, traccar_admin_api_token


_send_queue: asyncio.Queue[SendRequest | None] | None = None
_send_worker_task: asyncio.Task[None] | None = None
_send_http_clients: dict[int, httpx.AsyncClient] = {}
_last_send_monotonic = 0.0


async def _global_send_worker() -> None:
    global _last_send_monotonic, _send_http_clients

    if _send_queue is None:
        return

    while True:
        item = await _send_queue.get()
        if item is None:
            _send_queue.task_done()
            break

        user_id, url, data, headers, future = item
        success = False

        try:
            now = time.monotonic()
            elapsed = now - _last_send_monotonic
            if _last_send_monotonic > 0 and elapsed < GLOBAL_SEND_INTERVAL_SECONDS:
                await asyncio.sleep(GLOBAL_SEND_INTERVAL_SECONDS - elapsed)

            http_client = _send_http_clients.get(user_id)
            if http_client is None:
                http_client = httpx.AsyncClient(http2=True, timeout=10.0)
                _send_http_clients[user_id] = http_client

            resp = await http_client.post(url, data=data, headers=headers)
            success = 200 <= resp.status_code < 300
        except Exception:
            logging.exception("Error pushing location to Traccar")
            success = False
        finally:
            _last_send_monotonic = time.monotonic()
            if not future.cancelled():
                future.set_result(success)
            _send_queue.task_done()


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
    def __init__(
        self,
        traccar_push_url: str,
        traccar_admin_api_url: str,
        traccar_admin_api_token: str,
        user_id: int = 0,
        account_path: str | None = None,
        device_path: str = USER_DEVICE_FILE,
    ):
        self.user_id = user_id
        self.push_url = traccar_push_url
        self.admin_url = traccar_admin_api_url
        self.traccar_admin_api_token = traccar_admin_api_token
        if account_path is None:
            account_path = os.path.join(get_config_dir(), USER_ACCOUNT_FILE)
        self.account_path = account_path
        self.device_path = device_path
        self.account: AsyncAppleAccount | None = None
        self.accessories: list[FindMyAccessory] = []
        self.accessory_by_id: dict[str, FindMyAccessory] = {}
        self.device_states: dict[str, DeviceState] = {}

        self.queue: asyncio.Queue[FindMyAccessory] = asyncio.Queue()
        self.processing_ids: set[str] = set()

    async def load_accessories(self) -> None:
        if not os.path.exists(self.device_path):
            raise RuntimeError(f"Accessory file not found: {self.device_path}")

        with open(self.device_path, "r") as f:
            raw = json.load(f)

        if not isinstance(raw, list):
            raise RuntimeError(
                "Invalid format in devices.json; expected a list of accessory mappings"
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
        if os.path.exists(self.account_path):
            acc = AsyncAppleAccount.from_json(
                self.account_path, anisette_libs_path=libs_path
            )
        else:
            acc = AsyncAppleAccount(LocalAnisetteProvider(libs_path=libs_path))
            await _login_async(acc)
            acc.to_json(self.account_path)
        return acc

    async def upload_location(self, device_id: str, location: LocationReport) -> bool:
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

        data: dict[str, object] = {
            "id": device_id,
            "timestamp": location.timestamp.isoformat(),
            "lat": location.latitude,
            "lon": location.longitude,
            "accuracy": location.horizontal_accuracy,
            "confidence": confidence_int,
            "findmy_status": location.status,
        }

        if _send_queue is None:
            raise RuntimeError("Global sender is not started")

        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        await _send_queue.put(
            (
                self.user_id,
                self.push_url,
                data,
                {"Authorization": f"Bearer {self.traccar_admin_api_token}"},
                future,
            )
        )

        try:
            return await future
        except Exception:
            logging.exception("Error pushing location for %s", device_id)
            return False

    async def process_device_reports(
        self,
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

        tasks = [self.upload_location(did, loc) for loc in filtered]
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
        with open(self.device_path, "w") as f:
            json.dump(out, f, indent=4)

    async def worker(self) -> None:
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
                # fetch_location_history returns a list of location reports.
                reports = await self.account.fetch_location_history(acc)  # pyright: ignore[reportOptionalMemberAccess]

                if reports:
                    await self.process_device_reports(acc, reports)
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
                    old_path = self.account_path
                    backup_path = self.account_path + ".old"
                    if os.path.exists(old_path):
                        if os.path.exists(backup_path):
                            os.remove(backup_path)
                        os.rename(old_path, backup_path)
                    logging.error(
                        "Device ID: %s | Invalid session state; moved %s to %s for re-authentication",
                        did,
                        old_path,
                        backup_path,
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

        logging.info("Service loop started")
        asyncio.create_task(self.worker())

        try:
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
                self.account.to_json(self.account_path)


async def _login_async(account: AsyncAppleAccount) -> None:
    email = input("email?  > ")
    password = input("passwd? > ")

    state = cast(LoginState, await account.login(email, password))  # pyright: ignore[reportUnknownMemberType]

    if state == LoginState.REQUIRE_2FA:  # Account requires 2FA
        methods = cast(
            Sequence[TrustedDeviceSecondFactorMethod | SmsSecondFactorMethod],
            await account.get_2fa_methods(),  # pyright: ignore[reportUnknownMemberType]
        )

        # Print the (masked) phone numbers
        for i, method in enumerate(methods):
            if isinstance(method, SmsSecondFactorMethod):
                print(f"{i} - SMS ({method.phone_number})")
            else:
                print(f"{i} - Trusted Device")

        ind = int(input("Method? > "))
        method = methods[ind]

        await _maybe_await(method.request())

        code = input("Code? > ")

        # This automatically finishes the post-2FA login flow
        await _maybe_await(method.submit(code))


async def _maybe_await(result: object) -> None:
    if inspect.isawaitable(result):
        await cast(Awaitable[object], result)


def ensure_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def main() -> None:
    get_config_dir(create=True)
    traccar_push_url, traccar_admin_api_url, traccar_admin_api_token = load_config()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    global _send_queue, _send_worker_task, _send_http_clients, _last_send_monotonic
    _send_http_clients = {}
    _send_queue = asyncio.Queue()
    _last_send_monotonic = 0.0
    _send_worker_task = asyncio.create_task(_global_send_worker())

    users = discover_user_dirs()
    services = [
        LocationSyncService(
            traccar_push_url,
            traccar_admin_api_url,
            traccar_admin_api_token,
            user_id=user_id,
            account_path=account_path,
            device_path=device_path,
        )
        for user_id, account_path, device_path in users
    ]

    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(service.run()) for service in services
    ]
    try:
        pending: set[asyncio.Task[None]] = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_EXCEPTION
            )
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    logging.exception("User service task failed", exc_info=exc)
    except KeyboardInterrupt:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await _send_queue.join()
        await _send_queue.put(None)
        await _send_worker_task
        for http_client in _send_http_clients.values():
            await http_client.aclose()
        _send_queue = None
        _send_worker_task = None
        _send_http_clients = {}


if __name__ == "__main__":
    asyncio.run(main())
