"""ice_edge: the edge controller (reference architecture §2.1-2.10, §3, §5, §7).

Layers, high to low: app, web, uplink, views, sale, vend | payment | reporting, faults | notify,
monitoring | access, ledger | customer_ui | hal | config, supervisor | cctv. Higher layers may import lower ones.
"""
