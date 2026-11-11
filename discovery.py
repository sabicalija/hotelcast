"""
Device Discovery & Network Routing for HotelCast (`discovery.py`).

Provides:
- mDNS / Avahi discovery for `_airplay._tcp` and `_leboremote._tcp` services
- Zeroconf fallback discovery (if python-zeroconf is installed)
- TXT record parsing for `airplay=` and `lelinkport=` port overrides
- Manual `IP:Port` target parsing
- Physical Wi-Fi interface IPv4 detection & VPN bypass routing via `network.py`
- UFW firewall inspection and user guidance
"""

from __future__ import annotations

import ipaddress
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from network import (
    check_tv_reachable,
    ensure_vpn_bypass_route,
    find_physical_wifi_interface,
)


DEFAULT_AIRPLAY_PORT = 52266
SERVICE_TYPES = ("_airplay._tcp", "_leboremote._tcp")


@dataclass
class DiscoveredDevice:
    """Represents an AirPlay / HappyCast / LeLink receiver discovered on the LAN."""

    name: str
    ip: str
    port: int = DEFAULT_AIRPLAY_PORT
    service_type: str = "_airplay._tcp"
    hostname: str = ""
    txt: Dict[str, str] = field(default_factory=dict)

    @property
    def address(self) -> str:
        return f"{self.ip}:{self.port}"

    @property
    def display_name(self) -> str:
        svc_label = "AirPlay" if "airplay" in self.service_type else (
            "HappyCast/LeBo" if "lebo" in self.service_type else "Saved/Manual"
        )
        return f"{self.name} ({self.ip}:{self.port}) [{svc_label}]"


@dataclass
class FirewallStatus:
    """Represents the local UFW firewall state for the HTTP server port."""

    active: bool
    port_blocked: bool
    http_port: int
    warning: Optional[str] = None
    helper_command: Optional[str] = None


def unescape_avahi_string(val: str) -> str:
    """Unescape Avahi's decimal escape sequences like \\032 (space)."""
    def _replace(match: re.Match) -> str:
        code = int(match.group(1), 10)
        return chr(code) if 0 <= code < 256 else match.group(0)

    return re.sub(r"\\(\d{3})", _replace, val)


def parse_txt_tokens(raw_txt: str) -> Dict[str, str]:
    """
    Parse Avahi parsable TXT record fields such as:
    '"airplay=52266" "lelinkport=52266" "model=AppleTV3,2"'
    """
    result: Dict[str, str] = {}
    if not raw_txt:
        return result

    quoted = re.findall(r'"([^"]*)"', raw_txt)
    tokens = quoted if quoted else raw_txt.split(";")
    for token in tokens:
        token = unescape_avahi_string(token.strip())
        if "=" in token:
            k, v = token.split("=", 1)
            result[k.strip().lower()] = v.strip()
    return result


def extract_airplay_port(service_port: int, txt: Dict[str, str]) -> int:
    """
    Determine the target AirPlay/HappyCast HTTP port from TXT records or service port.
    Checks `airplay=` and `lelinkport=` in the TXT record first.
    """
    for key in ("airplay", "lelinkport", "airplayport"):
        if key in txt:
            try:
                port = int(txt[key])
                if 1 <= port <= 65535:
                    return port
            except (ValueError, TypeError):
                pass

    if 1 <= service_port <= 65535:
        return service_port
    return DEFAULT_AIRPLAY_PORT


def is_valid_ipv4(address: str) -> bool:
    """Return True if address is a valid IPv4 address."""
    try:
        ip = ipaddress.ip_address(address.strip())
        return isinstance(ip, ipaddress.IPv4Address) and not ip.is_multicast
    except ValueError:
        return False


def parse_avahi_parsable_output(output: str) -> List[DiscoveredDevice]:
    """
    Parse the output of `avahi-browse -r -p -t <service>`.

    Expected resolved line format:
    =;interface;protocol;name;type;domain;hostname;address;port;txt...
    """
    devices: List[DiscoveredDevice] = []
    seen: Dict[tuple[str, int], DiscoveredDevice] = {}

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("="):
            continue

        parts = line.split(";")
        if len(parts) < 9:
            continue

        proto = parts[2].strip()
        name = unescape_avahi_string(parts[3].strip())
        service_type = parts[4].strip()
        hostname = parts[6].strip()
        address = parts[7].strip()
        port_str = parts[8].strip()
        raw_txt = ";".join(parts[9:]) if len(parts) > 9 else ""

        if proto not in ("IPv4", "inet") and not is_valid_ipv4(address):
            continue
        if not is_valid_ipv4(address):
            continue

        try:
            service_port = int(port_str)
        except ValueError:
            service_port = DEFAULT_AIRPLAY_PORT

        txt = parse_txt_tokens(raw_txt)
        target_port = extract_airplay_port(service_port, txt)

        key = (address, target_port)
        dev = DiscoveredDevice(
            name=name or hostname or address,
            ip=address,
            port=target_port,
            service_type=service_type,
            hostname=hostname,
            txt=txt,
        )
        if key not in seen:
            seen[key] = dev
            devices.append(dev)
        else:
            existing = seen[key]
            existing.txt.update(txt)
            if "_airplay._tcp" in service_type:
                existing.service_type = service_type

    return devices


def discover_via_avahi(timeout: float = 4.0) -> List[DiscoveredDevice]:
    """Run `avahi-browse -r -p -t` for `_airplay._tcp` and `_leboremote._tcp`."""
    avahi_bin = shutil.which("avahi-browse")
    if not avahi_bin:
        return []

    combined_output: List[str] = []
    for svc in SERVICE_TYPES:
        try:
            proc = subprocess.run(
                [avahi_bin, "-r", "-p", "-t", svc],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if proc.stdout:
                combined_output.append(proc.stdout)
        except (subprocess.TimeoutExpired, OSError):
            continue

    return parse_avahi_parsable_output("\n".join(combined_output))


def discover_via_zeroconf(timeout: float = 3.0) -> List[DiscoveredDevice]:
    """Fallback discovery using python-zeroconf if installed."""
    try:
        import time
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError:
        return []

    discovered: List[DiscoveredDevice] = []
    seen: set[tuple[str, int]] = set()

    class _Listener(ServiceListener):
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name)
            if not info:
                return
            txt: Dict[str, str] = {}
            if info.properties:
                for k, v in info.properties.items():
                    key_str = k.decode("utf-8", errors="ignore") if isinstance(k, bytes) else str(k)
                    val_str = v.decode("utf-8", errors="ignore") if isinstance(v, bytes) else str(v or "")
                    txt[key_str.lower()] = val_str

            service_port = info.port or DEFAULT_AIRPLAY_PORT
            target_port = extract_airplay_port(service_port, txt)
            short_name = name.replace(f".{type_}", "").strip(".")

            for addr in info.parsed_addresses():
                if is_valid_ipv4(addr):
                    key = (addr, target_port)
                    if key not in seen:
                        seen.add(key)
                        discovered.append(
                            DiscoveredDevice(
                                name=short_name or addr,
                                ip=addr,
                                port=target_port,
                                service_type=type_.rstrip("."),
                                hostname=info.server or "",
                                txt=txt,
                            )
                        )

        def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

        def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            pass

    zc: Optional[Zeroconf] = None
    try:
        zc = Zeroconf()
        listener = _Listener()
        browsers = [
            ServiceBrowser(zc, f"{svc}.local.", listener) for svc in SERVICE_TYPES
        ]
        time.sleep(max(0.5, min(timeout, 5.0)))
        del browsers
    except Exception:
        pass
    finally:
        if zc is not None:
            try:
                zc.close()
            except Exception:
                pass

    return discovered


def _device_sort_key(dev: DiscoveredDevice, preferred_name: str = "") -> tuple[int, int, int, str]:
    """
    Rank dedicated TV Cast receivers (`_leboremote._tcp`, HappyCast ports, or matching
    `preferred_name`) ahead of personal computers advertising macOS AirPlay on port `7000`.
    """
    model = dev.txt.get("model", "").lower()
    is_computer = model.startswith(("mac", "imac"))
    matches_pref = bool(preferred_name and preferred_name.lower() in dev.name.lower())
    is_tv_cast = dev.service_type == "_leboremote._tcp" or (dev.port != 7000 and not is_computer)
    return (0 if matches_pref else 1, 0 if is_tv_cast else 1, 1 if is_computer else 0, dev.ip)


def discover_devices(
    timeout: float = 4.0,
    on_log: Optional[Callable[[str], None]] = None,
    saved_ip: str = "",
    saved_port: int = DEFAULT_AIRPLAY_PORT,
    saved_name: str = "",
) -> List[DiscoveredDevice]:
    """Scan LAN for `_airplay._tcp` and `_leboremote._tcp` receivers via Avahi, Zeroconf, and optional saved-TV probe."""
    if on_log:
        on_log("Scanning LAN for _airplay._tcp and _leboremote._tcp receivers...")

    devices_by_key: Dict[tuple[str, int], DiscoveredDevice] = {}

    # If the user has a saved TV IP configured, probe it directly via unicast TCP as well,
    # since hotel VLAN routers frequently drop mDNS multicast across subnets.
    clean_saved_ip = (saved_ip or "").strip()
    if clean_saved_ip and is_valid_ipv4(clean_saved_ip):
        if check_tv_reachable(clean_saved_ip, saved_port, timeout=0.6):
            devices_by_key[(clean_saved_ip, int(saved_port))] = DiscoveredDevice(
                name=(saved_name or "").strip() or f"Saved TV ({clean_saved_ip})",
                ip=clean_saved_ip,
                port=int(saved_port),
                service_type="saved",
            )

    for dev in discover_via_avahi(timeout=timeout):
        devices_by_key[(dev.ip, dev.port)] = dev

    if not devices_by_key:
        for dev in discover_via_zeroconf(timeout=min(timeout, 2.5)):
            devices_by_key.setdefault((dev.ip, dev.port), dev)

    results = sorted(
        devices_by_key.values(),
        key=lambda d: _device_sort_key(d, preferred_name=saved_name),
    )
    if on_log:
        if results:
            for d in results:
                on_log(f"Discovered TV: {d.display_name}")
        else:
            on_log("No AirPlay/HappyCast TVs discovered via mDNS (multicast may be blocked; enter TV IP:Port manually).")
    return results


def parse_manual_target(
    target: str,
    default_port: int = DEFAULT_AIRPLAY_PORT,
    name: Optional[str] = None,
) -> DiscoveredDevice:
    """
    Parse a manual 'IP:Port' or 'IP' string into a DiscoveredDevice.
    Raises ValueError if the IP or port is invalid.
    """
    cleaned = target.strip()
    if not cleaned:
        raise ValueError("Target IP:Port cannot be empty.")

    cleaned = re.sub(r"^https?://", "", cleaned).rstrip("/")

    if ":" in cleaned:
        host_part, port_part = cleaned.rsplit(":", 1)
        try:
            port = int(port_part)
        except ValueError as exc:
            raise ValueError(f"Invalid port in '{target}': {port_part}") from exc
    else:
        host_part = cleaned
        port = default_port

    if not (1 <= port <= 65535):
        raise ValueError(f"Port out of range (1-65535): {port}")

    host_part = host_part.strip()
    if host_part.lower() == "localhost":
        ip_str = "127.0.0.1"
    elif is_valid_ipv4(host_part):
        ip_str = host_part
    else:
        try:
            ip_str = socket.gethostbyname(host_part)
        except socket.gaierror as exc:
            raise ValueError(f"Cannot resolve host '{host_part}': {exc}") from exc

    return DiscoveredDevice(
        name=name or f"Room TV ({ip_str})",
        ip=ip_str,
        port=port,
        service_type="manual",
        hostname=host_part,
    )


def get_local_ip(
    tv_ip: str,
    tv_port: int = DEFAULT_AIRPLAY_PORT,
    on_log: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Determine the laptop's exact physical Wi-Fi LAN IP (`/sys/class/net/*/wireless`)
    for `Content-Location: http://<wifi_ip>:<port>/stream.m3u8`, bypassing active VPN tunnels
    (`wg*`, `tun*`, `tap*`).
    """
    target_is_loopback = tv_ip.startswith("127.") or tv_ip.lower() == "localhost"
    if target_is_loopback:
        return "127.0.0.1"

    # 1. Prefer physical wireless interface and ensure VPN bypass route is set
    wifi = find_physical_wifi_interface()
    if wifi and wifi.ipv4:
        ensure_vpn_bypass_route(tv_ip, wifi_info=wifi, on_log=on_log)
        return wifi.ipv4

    # 2. Fallback: dummy UDP socket probe
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((tv_ip, max(1, int(tv_port))))
        local_ip = sock.getsockname()[0]
        if local_ip and not local_ip.startswith("127."):
            return local_ip
    except OSError:
        pass
    finally:
        sock.close()

    return "127.0.0.1"


def check_firewall(
    http_port: int = 8000,
    on_log: Optional[Callable[[str], None]] = None,
) -> FirewallStatus:
    """
    Check if UFW is active and potentially blocking incoming traffic on `http_port`.
    """
    helper_cmd = f"sudo ufw allow {http_port}/tcp"
    ufw_bin = shutil.which("ufw") or "/usr/sbin/ufw"

    status_output = ""
    try:
        proc = subprocess.run(
            ["sudo", "-n", ufw_bin, "status"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            status_output = proc.stdout
    except (subprocess.TimeoutExpired, OSError):
        pass

    if not status_output:
        try:
            proc = subprocess.run(
                [ufw_bin, "status"],
                capture_output=True,
                text=True,
                timeout=1.5,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                status_output = proc.stdout
        except (subprocess.TimeoutExpired, OSError):
            pass

    if status_output:
        lower = status_output.lower()
        if "status: inactive" in lower:
            return FirewallStatus(active=False, port_blocked=False, http_port=http_port)
        if "status: active" in lower:
            port_pattern = re.compile(rf"\b{http_port}(/tcp)?\b.*allow", re.IGNORECASE)
            if port_pattern.search(status_output):
                return FirewallStatus(active=True, port_blocked=False, http_port=http_port)

            warning = (
                f"UFW firewall is active and port {http_port}/tcp may be blocked. "
                f"If the TV fails to fetch segments, run: {helper_cmd}"
            )
            if on_log:
                on_log(f"WARNING: {warning}")
            return FirewallStatus(
                active=True,
                port_blocked=True,
                http_port=http_port,
                warning=warning,
                helper_command=helper_cmd,
            )

    try:
        with open("/etc/ufw/ufw.conf", "r", encoding="utf-8") as f:
            conf = f.read()
        if re.search(r"^\s*ENABLED\s*=\s*yes\b", conf, re.IGNORECASE | re.MULTILINE):
            warning = (
                f"UFW firewall is enabled (/etc/ufw/ufw.conf). Ensure incoming traffic on "
                f"port {http_port}/tcp is permitted: {helper_cmd}"
            )
            if on_log:
                on_log(f"WARNING: {warning}")
            return FirewallStatus(
                active=True,
                port_blocked=True,
                http_port=http_port,
                warning=warning,
                helper_command=helper_cmd,
            )
    except OSError:
        pass

    return FirewallStatus(active=False, port_blocked=False, http_port=http_port)
