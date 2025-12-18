from __future__ import annotations

import asyncio
import logging
import os
import json
import time
import threading
import urllib.request
import urllib.parse
import hashlib
import base64
from datetime import datetime
from typing import Any

from findmy import (  # pyright: ignore[reportMissingTypeStubs]
    AsyncAppleAccount,
    FindMyAccessory,
    LocalAnisetteProvider,
    LoginState,
    SmsSecondFactorMethod,
    TrustedDeviceSecondFactorMethod,
)

STORE_PATH = "account.json"
last_uploads_file = "last_uploads.json"
PRUNE_SECONDS = 24 * 60 * 60
_last_uploads_lock = threading.Lock()
_last_uploads: dict[str, list[dict[str, Any]]] = {}

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


# --- Upload state management & Traccar upload helpers ---

def _load_state() -> None:
    """Load and prune the last uploads history from disk.

    Expects a mapping of device ids to lists of entries in the form:
        {'id': str, 'ts': float}
    """
    global _last_uploads
    try:
        if os.path.exists(last_uploads_file):
            with open(last_uploads_file, 'r') as f:
                data = json.load(f)
        else:
            data = {}
        cutoff_ts = time.time() - PRUNE_SECONDS
        processed: dict[str, list[dict[str, Any]]] = {}
        for k, v in data.items():
            entries: list[dict[str, Any]] = []
            try:
                # Expect entries as list of dicts with 'id' and 'ts'
                if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                    for x in v:
                        try:
                            rid = str(x.get("id"))
                            ts = float(x.get("ts", 0))
                            entries.append({"id": rid, "ts": ts})
                        except Exception:
                            continue
            except Exception:
                entries = []

            if not entries:
                continue

            # sort unique by ts ascending, keep only recent
            unique_map: dict[str, dict[str, Any]] = {}
            for e in entries:
                unique_map[e["id"]] = e
            unique_sorted = sorted(unique_map.values(), key=lambda x: x["ts"])
            recent = [e for e in unique_sorted if e["ts"] >= cutoff_ts]
            if not recent and unique_sorted:
                recent = [unique_sorted[-1]]
            if recent:
                processed[str(k)] = recent
        if not processed:
            _last_uploads = {}
        else:
            _last_uploads = processed
    except Exception:
        _last_uploads = {}


def _save_last_uploads_to_disk() -> None:
    """Persist the pruned upload history to disk."""
    try:
        with _last_uploads_lock:
            recent_uploads: dict[str, list[dict[str, Any]]] = {}
            for device_id, entries in _last_uploads.items():
                try:
                    # ensure entries are dicts with id and ts
                    processed_entries = [
                        {"id": str(e["id"]), "ts": float(e["ts"])} for e in entries
                    ]
                except Exception:
                    continue
                if processed_entries:
                    recent_uploads[device_id] = processed_entries
        try:
            dirpath = os.path.dirname(last_uploads_file)
            if dirpath and not os.path.exists(dirpath):
                os.makedirs(dirpath, exist_ok=True)
        except Exception:
            pass
        with open(last_uploads_file, 'w') as f:
            json.dump(recent_uploads, f)
    except Exception:
        pass


def _get_uploaded_report_ids(device_id: str) -> set[str]:
    with _last_uploads_lock:
        entries = _last_uploads.get(device_id)
        if not entries:
            return set()
        return {str(e["id"]) for e in entries}


def _set_last_upload_report(device_id: str, report_id: str, ts: float) -> None:
    with _last_uploads_lock:
        try:
            cur = _last_uploads.get(device_id, [])
            # replace / append by id
            cur_map: dict[str, dict[str, Any]] = {str(e["id"]): e for e in cur}
        except Exception:
            cur_map = {}
        cur_map[str(report_id)] = {"id": str(report_id), "ts": float(ts)}
        # sort by timestamp ascending
        unique_sorted = sorted(cur_map.values(), key=lambda x: x["ts"])
        now = time.time()
        cutoff_ts = now - PRUNE_SECONDS
        pruned = [e for e in unique_sorted if e["ts"] >= cutoff_ts]
        if not pruned and unique_sorted:
            pruned = [unique_sorted[-1]]
        _last_uploads[device_id] = pruned
    _save_last_uploads_to_disk()


def _upload_location(device_id: str, location: Any, push_url: str) -> bool:
    """Upload a single location (dict or object) to push_url as form-encoded data.

    Handles multiple location shapes (dicts, objects with attributes, protobuf-like objects with `seconds`).
    Returns True on success, False otherwise. Performs timestamp deduping.
    """
    if not push_url:
        raise RuntimeError('Push service URL not configured')
    if not location:
        raise RuntimeError('No valid location')

    def _get_field(obj: Any, *names: str):
        if isinstance(obj, dict):
            for n in names:
                if n in obj and obj[n] is not None:
                    return obj[n]
            return None
        for n in names:
            val = getattr(obj, n, None)
            if val is not None:
                return val
        return None

    lat = _get_field(location, 'latitude', 'lat')
    lon = _get_field(location, 'longitude', 'lon')
    if lat is None or lon is None:
        logging.debug('Skipping location without lat/lon')
        return False

    # prefer any horizontal_accuracy-like field
    accuracy = _get_field(location, 'accuracy', 'horizontal_accuracy', 'hdop')
    altitude = _get_field(location, 'altitude')
    status = _get_field(location, 'status')
    confidence = _get_field(location, 'confidence')

    # determine report id and timestamp (timestamp kept for pruning only)
    location_ts = None
    tval = _get_field(location, 'time', 'timestamp', 'timestamp_int')
    if tval is not None:
        try:
            if isinstance(tval, datetime):
                location_ts = float(tval.timestamp())
            elif hasattr(tval, 'seconds'):
                location_ts = float(getattr(tval, 'seconds'))
            elif isinstance(tval, (int, float)):
                location_ts = float(tval)
            else:
                location_ts = float(str(tval))
        except Exception:
            location_ts = None

    # compute a stable report id: prefer raw payload if available, else fallback to lat/lon+ts
    report_id = None
    try:
        payload = None
        if hasattr(location, 'payload'):
            payload = getattr(location, 'payload')
        elif isinstance(location, dict) and 'payload' in location:
            payload = location['payload']
        if isinstance(payload, str):
            # likely base64-encoded string; prefer to decode if possible
            try:
                payload = base64.b64decode(payload)
            except Exception:
                payload = payload.encode('utf-8')
        if isinstance(payload, (bytes, bytearray)):
            report_id = hashlib.sha256(bytes(payload)).hexdigest()
        else:
            # fallback to combining fields
            report_id = hashlib.sha256(f"{lat}:{lon}:{location_ts}".encode('utf-8')).hexdigest()
    except Exception:
        report_id = hashlib.sha256(f"{lat}:{lon}:{location_ts}".encode('utf-8')).hexdigest()

    uploaded_ids = _get_uploaded_report_ids(device_id)
    if report_id in uploaded_ids:
        logging.debug(f"Skipping upload for {device_id}: report id {report_id} already uploaded (history size={len(uploaded_ids)})")
        return False

    data: dict[str, Any] = {
        'id': device_id,
        'lat': str(lat),
        'lon': str(lon),
    }
    # map horizontal accuracy/confidence into traccar fields
    if accuracy is not None:
        data['accuracy'] = str(accuracy)
    if confidence is not None:
        # keep both a discoverable field and a namespaced one
        data['confidence'] = str(confidence)
        data['findhub_confidence'] = str(confidence)
    if altitude is not None:
        data['altitude'] = str(altitude)
    if location_ts is not None:
        data['timestamp'] = str(location_ts)
    if status is not None:
        data['findhub_type'] = str(status)
        data['findhub_status'] = str(status)
    try:
        encoded = urllib.parse.urlencode(data).encode('utf-8')
        req = urllib.request.Request(push_url, data=encoded, method='POST')
        with urllib.request.urlopen(req, timeout=10) as resp:
            code = getattr(resp, 'getcode', lambda: None)()
            if code and 200 <= code < 300:
                try:
                    _set_last_upload_report(device_id, report_id, location_ts if location_ts is not None else time.time())
                except Exception:
                    pass
                return True
            else:
                logging.debug(f"Push failed for {device_id}, status: {code}")
                return False
    except Exception:
        logging.debug(f"Error pushing location for {device_id}", exc_info=True)
        return False


def _device_identifier(airtag: Any) -> str:
    # Try to pick a reasonable identifier for the tracked accessory
    # Prefer an explicit "identifier" field from the accessory JSON
    for attr in ("identifier", "serial", "device_id", "id", "name", "airtag_id"):
        try:
            v = getattr(airtag, attr)
            if v:
                return str(v)
        except Exception:
            pass
    # Fallback to airtag filename base
    return os.path.splitext(os.path.basename("airtag.json"))[0]




async def main_sync():
    airtag_path = "airtag.json"

    # Step 0: create an accessory key generator
    airtag = FindMyAccessory.from_json(airtag_path)

    # Step 1: log into an Apple account
    acc = await get_account_async()
    print(f"Logged in as: {acc.account_name} ({acc.first_name} {acc.last_name})")

    # step 2: fetch reports!
    locations = await acc.fetch_location_history(airtag)

    # Upload to Traccar/Push URL if configured
    push_url = os.environ.get('PUSH_URL') or os.environ.get('TRACCAR_URL')
    if push_url:
        _load_state()
        device_id = _device_identifier(airtag)
        device_name = airtag.name
        uploaded = 0
        tried = 0
        for loc in locations or []:
            tried += 1
            try:
                if _upload_location(device_id, loc, push_url):
                    uploaded += 1
            except Exception:
                logging.exception('Error while uploading a location')
        logging.info(f"Uploaded {uploaded}/{tried} locations to {push_url} for device {device_id} ('{device_name}')")
        print(f"Device ID: {device_id} | Name: {device_name} | Uploaded: {uploaded}/{tried} locations")
    # step 4: save current account state to disk
    try:
        _save_last_uploads_to_disk()
    except Exception:
        logging.debug('Failed to save last uploads state', exc_info=True)

    await acc.close()
    acc.to_json(STORE_PATH)
    airtag.to_json(airtag_path)


def main():
    """CLI entrypoint

    Supports the same argument names as the Google Find Hub script for compatibility:
      --push-url             (env fallback: PUSH_URL)
      --traccar-device-id    (env fallback: TRACCAR_DEVICE_ID)
    """
    import argparse

    parser = argparse.ArgumentParser(description="Apple Find My -> Traccar uploader")
    parser.add_argument('--push-url', default=os.environ.get('PUSH_URL'), help='URL to which locations are uploaded (env: PUSH_URL)')

    args = parser.parse_args()

    # Apply CLI values to environment or globals so the rest of the code can use them
    if args.push_url:
        os.environ['PUSH_URL'] = args.push_url

    return asyncio.run(main_sync())