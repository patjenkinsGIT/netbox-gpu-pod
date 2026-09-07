#!/usr/bin/env python3
"""
Print the type slugs NetBox will accept for interfaces and power ports.

The spec has to name types by their API value ("infiniband-ndr4x", not
"NDR 4X (400 Gbps)"), and those values differ between NetBox versions.
Reading them off the live instance beats guessing and then debugging a
400 response in the middle of a build.

Usage:
    python scripts/show_choices.py
"""

from netbox_client import connect

nb, info = connect()
print(f"{info['url']}  (NetBox {nb.status()['netbox-version']})\n")

# Show the shape of a choice entry, so a future key rename is obvious
# rather than a KeyError three layers down.
_sample = nb.dcim.interfaces.choices()["type"][0]
print(f"choice entry keys: {sorted(_sample.keys())}\n")


def label_of(choice):
    """The human-readable half of a choice.

    NetBox has used 'display', 'label' and 'display_name' for this across
    versions, so take whichever is present rather than assuming one.
    """
    for key in ("display", "label", "display_name", "name"):
        if key in choice:
            return choice[key]
    return ""


def dump(endpoint, field, title, match=None):
    """Print value/label pairs, optionally filtered to a substring."""
    choices = endpoint.choices()[field]
    rows = [
        c for c in choices
        if match is None or match.lower() in f"{c['value']} {label_of(c)}".lower()
    ]
    print(f"--- {title} ({len(rows)} shown) ---")
    for c in rows:
        print(f"  {c['value']:<32} {label_of(c)}")
    print()


# InfiniBand -- for the DGX rails, QM9700 fabric ports and storage fabric.
dump(nb.dcim.interfaces, "type", "interface types: InfiniBand", match="infiniband")

# 100GbE -- SN4600C in-band fabric and SN2201 uplinks.
dump(nb.dcim.interfaces, "type", "interface types: 100G", match="100g")

# 1GbE copper -- BMC / out-of-band to the SN2201.
dump(nb.dcim.interfaces, "type", "interface types: 1000BASE", match="1000base")

# Power port types -- switch PSU inlets.
dump(nb.dcim.power_ports, "type", "power port types: IEC 60320", match="iec-60320")

# Device airflow, so the spec uses the right value.
dump(nb.dcim.device_types, "airflow", "device type airflow")

# --- phase 2: GB200 NVL72 -------------------------------------------------
#
# The liquid-cooled pod needs types the H100 model never used: 400G and 800G
# Ethernet for the BlueField-3 / SN5600 storage fabric, and something to
# represent a 50 V DC busbar drop rather than an IEC inlet. Same rule as
# above -- a slug read off the instance, not one recalled from a datasheet.

# BlueField-3 storage / in-band ports.
dump(nb.dcim.interfaces, "type", "interface types: 400G", match="400g")

# SN5600 is a 64-port 800 GbE switch -- confirm 4.7 can express that at all.
dump(nb.dcim.interfaces, "type", "interface types: 800G", match="800g")

# The NVL72 distributes 50-51 V DC over a busbar, not AC to each tray. If
# NetBox has no DC connector type, the tray input stays untyped -- which is
# what the rPDU `input` port already does, and is a finding rather than a
# blocker.
dump(nb.dcim.power_ports, "type", "power port types: DC", match="dc")
dump(nb.dcim.power_outlets, "type", "power outlet types: DC", match="dc")
