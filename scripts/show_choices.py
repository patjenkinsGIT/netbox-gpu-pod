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
