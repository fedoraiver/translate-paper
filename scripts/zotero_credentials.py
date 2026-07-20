#!/usr/bin/env python3
"""Store Zotero Web API credentials in Windows Credential Manager.

The secret is accepted only on stdin and is never printed. Other skill scripts
can import ``read_credential`` to obtain it inside the current process.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import sys
import urllib.request


DEFAULT_TARGET = "Codex/translate-paper/ZoteroAPI"
LEGACY_TARGET = "Codex/translate-paper-to-zotero/ZoteroAPI"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", wintypes.LPVOID),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _advapi32() -> ctypes.WinDLL:
    if os.name != "nt":
        raise RuntimeError("Windows Credential Manager is available only on Windows")

    library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    library.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    library.CredWriteW.restype = wintypes.BOOL
    library.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
    ]
    library.CredReadW.restype = wintypes.BOOL
    library.CredFree.argtypes = [wintypes.LPVOID]
    library.CredFree.restype = None
    return library


def write_credential(target: str, username: str, secret: str) -> None:
    if not target:
        raise ValueError("credential target must not be empty")
    if not username:
        raise ValueError("Zotero user ID must not be empty")
    if not secret:
        raise ValueError("Zotero API key must not be empty")

    secret_bytes = secret.encode("utf-16-le")
    secret_buffer = ctypes.create_string_buffer(secret_bytes)
    credential = _CREDENTIALW()
    credential.Type = _CRED_TYPE_GENERIC
    credential.TargetName = target
    credential.Comment = "Zotero Web API key for Translate paper"
    credential.CredentialBlobSize = len(secret_bytes)
    credential.CredentialBlob = ctypes.cast(
        secret_buffer, ctypes.POINTER(ctypes.c_ubyte)
    )
    credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
    credential.UserName = username

    library = _advapi32()
    if not library.CredWriteW(ctypes.byref(credential), 0):
        raise ctypes.WinError(ctypes.get_last_error())


def _read_exact_credential(target: str) -> tuple[str, str]:
    library = _advapi32()
    pointer = ctypes.POINTER(_CREDENTIALW)()
    if not library.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        error = ctypes.get_last_error()
        if error == _ERROR_NOT_FOUND:
            raise FileNotFoundError(f"credential not found: {target}")
        raise ctypes.WinError(error)

    try:
        credential = pointer.contents
        blob = ctypes.string_at(
            credential.CredentialBlob, credential.CredentialBlobSize
        )
        return credential.UserName, blob.decode("utf-16-le")
    finally:
        library.CredFree(pointer)


def read_credential(target: str = DEFAULT_TARGET) -> tuple[str, str]:
    try:
        return _read_exact_credential(target)
    except FileNotFoundError:
        if target == DEFAULT_TARGET:
            return _read_exact_credential(LEGACY_TARGET)
        raise


def credential_exists(target: str = DEFAULT_TARGET) -> tuple[bool, str | None]:
    try:
        username, _ = read_credential(target)
    except FileNotFoundError:
        return False, None
    return True, username


def verify_credential(target: str = DEFAULT_TARGET) -> dict[str, object]:
    user_id, api_key = read_credential(target)
    request = urllib.request.Request(
        "https://api.zotero.org/keys/current",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Zotero-API-Version": "3",
            "User-Agent": "translate-paper/2.0",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)

    returned_user_id = str(payload.get("userID", ""))
    if returned_user_id != user_id:
        raise RuntimeError("stored Zotero user ID does not match the API key owner")
    return {
        "valid": True,
        "user_id": returned_user_id,
        "access": payload.get("access", {}),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage the Zotero API key in Windows Credential Manager."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    store = subparsers.add_parser("store", help="Read a key from stdin and store it")
    store.add_argument("--user-id", required=True)
    store.add_argument("--target", default=DEFAULT_TARGET)

    check = subparsers.add_parser("check", help="Report whether a key is stored")
    check.add_argument("--target", default=DEFAULT_TARGET)

    verify = subparsers.add_parser(
        "verify", help="Verify the stored key without printing it"
    )
    verify.add_argument("--target", default=DEFAULT_TARGET)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.command == "store":
        secret = sys.stdin.read().rstrip("\r\n")
        write_credential(args.target, args.user_id, secret)
        print(
            json.dumps({"stored": True, "target": args.target, "user_id": args.user_id})
        )
        return 0

    if args.command == "check":
        exists, username = credential_exists(args.target)
        print(
            json.dumps({"stored": exists, "target": args.target, "user_id": username})
        )
        return 0 if exists else 1

    print(json.dumps(verify_credential(args.target)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
