import requests
import json
import sys
import logging
import urllib3
import re

# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(stream=sys.stderr, level=logging.ERROR)

def log(msg):
    print(msg, file=sys.stderr)

# -------------------------------
# Load inventory
# -------------------------------
with open("/scripts/F5_Pair.json") as f:
    data = json.load(f)

creds = data["credentials"]
ha_pairs = data["ha_pairs"]

rows = []

# -------------------------------
# Helper functions
# -------------------------------
def api_get(host, endpoint):
    url = f"https://{host}{endpoint}"
    r = requests.get(
        url,
        auth=(creds["username"], creds["password"]),
        verify=False,
        timeout=20
    )
    r.raise_for_status()
    return r.json()

def get_metric(entries, metric_name):
    for _, v in entries.items():
        nested = v.get("nestedStats", {}).get("entries", {})
        for item in nested.values():
            if item.get("description") == metric_name:
                return nested.get("Current", {}).get("description", "N/A")
    return "N/A"

def parse_ha_state(api_raw):
    m = re.search(
        r"Failover\s+(active|standby|offline|inoperative|disconnected|standalone|force offline)\s+for\s+(\d+)d\s+([\d:]+)",
        api_raw,
        re.IGNORECASE
    )
    if not m:
        return "UNKNOWN", "N/A"

    role = m.group(1).upper()
    uptime = f"{m.group(2)}d {m.group(3)}"
    return role, uptime

def percent_value(val):
    try:
        return int(val.replace('%', '').strip())
    except:
        return None

# -------------------------------
# Process HA pairs
# -------------------------------
for pair in ha_pairs:
    pair_name = pair["pair_name"]
    pair_roles = []
    pair_devices = []

    # ---- Per device collection ----
    for d in pair["devices"]:
        host = d["host"]
        hostname = d.get("hostname")

        issues = []
        severity = "OK"

        try:
            # ---- HA state ----
            failover = api_get(host, "/mgmt/tm/sys/failover")
            api_raw = failover.get("apiRawValues", {}).get("apiAnonymous", "")

            role, ha_uptime = parse_ha_state(api_raw)
            pair_roles.append(role)

            if role not in ["ACTIVE", "STANDBY"]:
                issues.append(f"HA={role}")
                severity = "CRITICAL"

            # ---- System performance ----
            perf = api_get(host, "/mgmt/tm/sys/performance/system")
            entries01 = perf["entries"]

            cpu_current = get_metric(entries01, "Utilization")
            tmm_mem     = get_metric(entries01, "TMM Memory Used")
            other_mem   = get_metric(entries01, "Other Memory Used")
            swap_used   = get_metric(entries01, "Swap Used")

            cpu_val = percent_value(cpu_current)
            tmm_val = percent_value(tmm_mem)

            if tmm_val is not None and tmm_val >= 80:
                issues.append(f"TMM Memory={tmm_val}%")
                if severity != "CRITICAL":
                    severity = "WARNING"

            if cpu_val is not None and cpu_val >= 85:
                issues.append(f"CPU={cpu_val}%")
                if severity != "CRITICAL":
                    severity = "WARNING"

            # ---- Connection metrics ----
            connection = api_get(host, "/mgmt/tm/sys/performance/connections")
            entries02 = connection["entries"]

            connection_current = get_metric(entries02, "Connections")
            connection_client  = get_metric(entries02, "Client Connections")
            connection_server  = get_metric(entries02, "Server Connections")
            http_request       = get_metric(entries02, "HTTP Requests")

            warning = (
                f"{severity}: " + " | ".join(issues)
                if issues else None
            )

            pair_devices.append({
                "pair": pair_name,
                "device_ip": host,
                "host_name": hostname,
                "ha_role": role,
                "ha_uptime": ha_uptime,
                "device_severity": severity,
                "device_warning": warning,
                "cpu_utilization": cpu_current,
                "tmm_memory_used": tmm_mem,
                "other_memory_used": other_mem,
                "swap_used": swap_used,
                "current_connections": connection_current,
                "server_side_connections": connection_server,
                "client_side_connections": connection_client,
                "http_requests": http_request
            })

        except Exception as e:
            log(f"ERROR connecting to {host}: {e}")
            pair_roles.append("UNREACHABLE")

    # -------------------------------
    # Pair-level HA validation
    # -------------------------------
    active_count = pair_roles.count("ACTIVE")
    standby_count = pair_roles.count("STANDBY")

    if len(pair_roles) != 2:
        pair_status = "CRITICAL"
        pair_reason = "Incomplete HA pair"
    elif active_count == 1 and standby_count == 1:
        pair_status = "OK"
        pair_reason = None
    else:
        pair_status = "CRITICAL"
        pair_reason = f"Invalid HA roles: {pair_roles}"

    # ---- Inject HA status into each device row ----
    for dev in pair_devices:
        dev["ha_pair_status"] = pair_status
        dev["ha_pair_reason"] = pair_reason
        rows.append(dev)

# -------------------------------
# Final JSON (single insert-ready array)
# -------------------------------
print(json.dumps({
    "status": "success",
    "pairs_processed": len(ha_pairs),
    "devices_processed": len(rows),
    "device_data": rows
}, indent=2))