#!/usr/bin/env python3
"""
Preflight check for the pod generator.

Confirms that NETBOX_URL / NETBOX_TOKEN in .env reach a live NetBox instance
and that the credential carries write scope. Run this before build_pod.py
whenever something looks wrong -- it isolates "can I talk to NetBox at all?"
from "is my generator logic correct?", which are very different problems.

Usage:
    python scripts/check_connection.py
"""

import sys

import pynetbox
import requests

from netbox_client import ConfigError, connect


def fail(msg):
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


try:
    nb, info = connect()
except ConfigError as exc:
    fail(str(exc))

print(f"url:    {info['url']}")
print(f"token:  {info['masked']}")
print(f"scheme: {info['scheme']}")

try:
    status = nb.status()
except requests.exceptions.ConnectionError:
    fail(f"Cannot reach {info['url']}. Is the stack up? (docker compose ps)")
except pynetbox.RequestError as exc:
    fail(f"NetBox rejected the request: {exc}")

print(f"netbox: {status['netbox-version']}")

# Read check.
print(f"racks:   {[r.name for r in nb.dcim.racks.all()]}")
print(f"devices: {[d.name for d in nb.dcim.devices.all()]}")

# Write check. A read-only credential passes every read above and then fails
# on the first create() -- which in a generator means dying partway through a
# run with a half-built pod. Better to find out against a throwaway object.
try:
    probe = nb.extras.tags.create(name="_preflight_probe", slug="_preflight_probe")
    probe.delete()
    print("write:  ok (created and deleted a throwaway tag)")
except pynetbox.RequestError as exc:
    fail(f"Credential cannot write: {exc}")

print("\nPreflight OK.")
