"""
VPN Bypass, Physical Wi-Fi Interface Pinning & Hotel Portal Activation (`network.py`).

Provides:
- Physical wireless interface detection (`/sys/class/net/*/wireless` or `phy80211`),
  ignoring virtual VPN/tunnel interfaces (`wg*`, `tun*`, `tap*`, `Meta`, `mihomo`, `docker*`, `veth*`).
- Exact Wi-Fi IPv4 address extraction via `ip -4 -j addr show <wifi_iface>`.
- Automatic VPN bypass subnet/host route injection (`ip route replace <tv_subnet>/24 via <wifi_gateway> dev <wifi_iface>`)
  to defeat WireGuard `suppress_prefixlength 0` policy routing while keeping the VPN connected.
- Proxy-bypassing (`ProxyHandler({})`) + Wi-Fi-pinned HTTP/HTTPS handlers so hotel room-binding
  portal requests route directly over the hotel Wi-Fi even while a VPN or HTTP proxy is active.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple


VPN_PREFIXES = (
    "wg",
    "tun",
    "tap",
    "ppp",
    "utun",
    "tailscale",
    "zt",
    "nordlynx",
    "proton",
    "meta",
    "mihomo",
    "clash",
    "sing",
    "cscotun",
)
VIRTUAL_PREFIXES = ("lo", "docker", "veth", "br-", "virbr", "vmnet", "vboxnet")
PREFERRED_WIFI_IFACES = ("wlan0", "wlo1", "wlp2s0", "wlp3s0", "wlp6s0")
DEFAULT_PORTAL_BASE_URL = "https://ovk.top"

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class WiFiInterfaceInfo:
    """Represents the detected physical wireless network interface."""

    ifname: str
    ipv4: str
    gateway: Optional[str] = None
    subnet_prefix: Optional[str] = None


def is_vpn_or_virtual_iface(ifname: str) -> bool:
    """Return True if `ifname` is a VPN tunnel, loopback, or container bridge interface."""
    lower = ifname.strip().lower()
    return lower.startswith(VPN_PREFIXES) or lower.startswith(VIRTUAL_PREFIXES)


def is_wireless_iface(ifname: str) -> bool:
    """Return True if `/sys/class/net/<ifname>/wireless` or `phy80211` exists, or name matches `wl*`."""
    sys_net = f"/sys/class/net/{ifname}"
    if os.path.isdir(os.path.join(sys_net, "wireless")) or os.path.isdir(
        os.path.join(sys_net, "phy80211")
    ):
        return True
    lower = ifname.strip().lower()
    return lower.startswith(("wl", "wlan", "wifi", "ath"))


def get_iface_ipv4(ifname: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Read the IPv4 address specifically assigned to `ifname` via `ip -4 -j addr show <ifname>`.
    Returns `(ipv4_address, cidr_prefix_len)` or `(None, None)`.
    """
    ip_bin = shutil.which("ip") or "/sbin/ip"
    try:
        proc = subprocess.run(
            [ip_bin, "-4", "-j", "addr", "show", ifname],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            for entry in data:
                for addr_info in entry.get("addr_info", []):
                    local_ip = addr_info.get("local")
                    prefixlen = addr_info.get("prefixlen")
                    if local_ip and not local_ip.startswith("127."):
                        return str(local_ip), str(prefixlen) if prefixlen is not None else "24"
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        pass
    return None, None


def get_iface_gateway(ifname: str, local_ip: Optional[str] = None) -> Optional[str]:
    """
    Read the IPv4 default/subnet gateway for `ifname` via `ip -4 -j route show dev <ifname>`
    (watching all routing tables including `table all`), falling back to `<subnet>.1`.
    """
    ip_bin = shutil.which("ip") or "/sbin/ip"
    for extra_args in ([], ["table", "all"]):
        try:
            proc = subprocess.run(
                [ip_bin, "-4", "-j", "route", "show", "dev", ifname] + extra_args,
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                routes = json.loads(proc.stdout)
                for route in routes:
                    gw = route.get("gateway")
                    if gw and not str(gw).startswith("127."):
                        return str(gw)
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            pass

    nmcli_bin = shutil.which("nmcli")
    if nmcli_bin:
        try:
            proc = subprocess.run(
                [nmcli_bin, "-g", "IP4.GATEWAY", "device", "show", ifname],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
            gw = proc.stdout.strip()
            if proc.returncode == 0 and gw and re.match(r"^\d+\.\d+\.\d+\.\d+$", gw):
                return gw
        except (subprocess.TimeoutExpired, OSError):
            pass

    if local_ip and re.match(r"^\d+\.\d+\.\d+\.\d+$", local_ip):
        octets = local_ip.split(".")
        return f"{octets[0]}.{octets[1]}.{octets[2]}.1"

    return None


def find_physical_wifi_interface() -> Optional[WiFiInterfaceInfo]:
    """
    Identify the active physical wireless interface via `/sys/class/net/*/wireless`
    (or common `wl*` interface names) connected to the LAN, ignoring `wg*`, `tun*`,
    `tap*`, and container bridges.
    """
    candidates: List[str] = []

    try:
        for entry in sorted(os.listdir("/sys/class/net")):
            if not is_vpn_or_virtual_iface(entry) and is_wireless_iface(entry):
                candidates.append(entry)
    except OSError:
        pass

    for pref in PREFERRED_WIFI_IFACES:
        if os.path.exists(f"/sys/class/net/{pref}") and pref not in candidates:
            candidates.append(pref)

    for ifname in candidates:
        ipv4, prefix = get_iface_ipv4(ifname)
        if ipv4:
            gw = get_iface_gateway(ifname, local_ip=ipv4)
            return WiFiInterfaceInfo(
                ifname=ifname,
                ipv4=ipv4,
                gateway=gw,
                subnet_prefix=prefix,
            )

    try:
        for entry in sorted(os.listdir("/sys/class/net")):
            if entry in candidates or is_vpn_or_virtual_iface(entry):
                continue
            ipv4, prefix = get_iface_ipv4(entry)
            if ipv4:
                gw = get_iface_gateway(entry, local_ip=ipv4)
                return WiFiInterfaceInfo(
                    ifname=entry,
                    ipv4=ipv4,
                    gateway=gw,
                    subnet_prefix=prefix,
                )
    except OSError:
        pass

    return None


def get_route_interface_for_ip(target_ip: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Return `(out_dev, src_ip)` currently chosen by the kernel routing table for `target_ip`.
    """
    ip_bin = shutil.which("ip") or "/sbin/ip"
    try:
        proc = subprocess.run(
            [ip_bin, "-4", "-j", "route", "get", target_ip],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            if data and isinstance(data, list):
                dev = data[0].get("dev")
                src = data[0].get("prefsrc")
                return (str(dev) if dev else None), (str(src) if src else None)
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        pass
    return None, None


def has_explicit_wifi_route(target_ip: str, wifi: WiFiInterfaceInfo) -> bool:
    """
    Check if `target_ip` has a non-default route (prefix > 0) in `table main` on `wifi.ifname`.
    WireGuard (`wg-quick`) uses `ip rule add table main suppress_prefixlength 0`, which
    suppresses the `default` route on the Wi-Fi NIC unless an explicit `/24` or `/32` route exists.
    """
    try:
        target_addr = ipaddress.IPv4Address(target_ip)
    except ValueError:
        return True

    ip_bin = shutil.which("ip") or "/sbin/ip"
    try:
        proc = subprocess.run(
            [ip_bin, "-4", "-j", "route", "show", "dev", wifi.ifname],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            routes = json.loads(proc.stdout)
            for r in routes:
                dst = r.get("dst")
                if not dst or dst == "default" or dst.startswith("0.0.0.0"):
                    continue
                try:
                    net = (
                        ipaddress.IPv4Network(dst, strict=False)
                        if "/" in dst
                        else ipaddress.IPv4Network(f"{dst}/32", strict=False)
                    )
                    if target_addr in net:
                        return True
                except ValueError:
                    continue
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        pass
    return False


def build_vpn_bypass_route_cmd(target_ip: str, wifi: WiFiInterfaceInfo) -> List[str]:
    """
    Build `ip route replace <target_subnet>/24 via <wifi_gateway> dev <wifi_iface>`
    so hotel LAN / VLAN targets route directly via Wi-Fi instead of a VPN tunnel.
    """
    ip_bin = shutil.which("ip") or "/sbin/ip"
    octets = target_ip.split(".")
    subnet_cidr = f"{octets[0]}.{octets[1]}.{octets[2]}.0/24" if len(octets) == 4 else f"{target_ip}/32"

    if wifi.gateway:
        return [
            ip_bin, "route", "replace", subnet_cidr,
            "via", wifi.gateway, "dev", wifi.ifname, "src", wifi.ipv4,
        ]
    return [ip_bin, "route", "replace", subnet_cidr, "dev", wifi.ifname, "src", wifi.ipv4]


def ensure_vpn_bypass_route(
    target_ip: str,
    wifi_info: Optional[WiFiInterfaceInfo] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> Optional[WiFiInterfaceInfo]:
    """
    Ensure `target_ip` has an explicit `/24` route in `table main` via the physical
    Wi-Fi gateway, bypassing WireGuard / OpenVPN policy routing.
    """
    if target_ip.startswith("127.") or target_ip.lower() == "localhost":
        return WiFiInterfaceInfo(ifname="lo", ipv4="127.0.0.1")

    wifi = wifi_info or find_physical_wifi_interface()
    if not wifi:
        return None

    routed_dev, routed_src = get_route_interface_for_ip(target_ip)
    explicit_ok = has_explicit_wifi_route(target_ip, wifi)

    if (routed_dev and routed_dev != wifi.ifname) or (routed_src and routed_src != wifi.ipv4) or not explicit_ok:
        route_args = build_vpn_bypass_route_cmd(target_ip, wifi)
        cmd_str = " ".join(route_args)
        if on_log:
            on_log(
                f"Adding hotel subnet bypass route so {target_ip} routes via {wifi.ifname} ({wifi.gateway})..."
            )

        success = False
        # 1. Try passwordless sudo -n or direct root execution
        for prefix_cmd in ([], ["sudo", "-n"]):
            try:
                proc = subprocess.run(
                    prefix_cmd + route_args,
                    capture_output=True,
                    text=True,
                    timeout=1.5,
                    check=False,
                )
                if proc.returncode == 0:
                    success = True
                    if on_log:
                        on_log(f"Applied VPN bypass route: {cmd_str}")
                    break
            except (subprocess.TimeoutExpired, OSError):
                continue

        # 2. If running in an interactive terminal, run `sudo ip route replace ...`
        if not success and sys.stdin and sys.stdin.isatty():
            try:
                proc = subprocess.run(["sudo"] + route_args, timeout=30.0, check=False)
                if proc.returncode == 0:
                    success = True
                    if on_log:
                        on_log(f"Applied VPN bypass route via sudo: {cmd_str}")
            except (subprocess.TimeoutExpired, OSError):
                pass

        # 3. If running in GUI mode without terminal, try pkexec graphical auth dialog
        if not success and os.environ.get("DISPLAY") and shutil.which("pkexec"):
            try:
                proc = subprocess.run(
                    ["pkexec"] + route_args,
                    capture_output=True,
                    text=True,
                    timeout=30.0,
                    check=False,
                )
                if proc.returncode == 0:
                    success = True
                    if on_log:
                        on_log(f"Applied VPN bypass route via pkexec: {cmd_str}")
            except (subprocess.TimeoutExpired, OSError):
                pass

        if not success and on_log:
            on_log(
                f"To route hotel TV traffic around your VPN, run once:\n"
                f"  sudo {cmd_str}"
            )

    return wifi


def bind_socket_to_wifi(
    sock: socket.socket,
    wifi_iface: Optional[str] = None,
    wifi_ip: Optional[str] = None,
    target_ip: Optional[str] = None,
) -> None:
    """
    Pin an outgoing TCP/UDP socket to the physical Wi-Fi interface:
    1. Attempts `socket.SO_BINDTODEVICE` to `wifi_iface` (works when root or `CAP_NET_RAW`).
    2. Binds the socket source IP to `(wifi_ip, 0)` so traffic originates from the Wi-Fi NIC.
    """
    if target_ip and (target_ip.startswith("127.") or target_ip.lower() == "localhost"):
        return

    if wifi_iface and wifi_iface != "lo" and hasattr(socket, "SO_BINDTODEVICE"):
        try:
            sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_BINDTODEVICE,
                wifi_iface.encode("utf-8") + b"\0",
            )
            return
        except OSError:
            pass

    if wifi_ip and not wifi_ip.startswith("127."):
        try:
            sock.bind((wifi_ip, 0))
        except OSError:
            pass


class WiFiPinnedHTTPConnection(http.client.HTTPConnection):
    """
    `http.client.HTTPConnection` subclass that pins its underlying socket to the
    physical Wi-Fi interface (`SO_BINDTODEVICE` and/or `source_address=(wifi_ip, 0)`).
    """

    def __init__(
        self,
        host: str,
        port: Optional[int] = None,
        timeout: float = 10.0,
        wifi_iface: Optional[str] = None,
        wifi_ip: Optional[str] = None,
        **kwargs: object,
    ) -> None:
        self.wifi_iface = wifi_iface
        self.wifi_ip = wifi_ip
        is_loopback = host.startswith("127.") or host.lower() == "localhost"
        src_addr = (wifi_ip, 0) if (wifi_ip and not is_loopback and not wifi_ip.startswith("127.")) else None
        super().__init__(host, port=port, timeout=timeout, source_address=src_addr)

    def connect(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        bind_socket_to_wifi(
            sock,
            wifi_iface=self.wifi_iface,
            wifi_ip=self.wifi_ip,
            target_ip=self.host,
        )
        sock.connect((self.host, self.port))
        self.sock = sock


class WiFiPinnedHTTPSConnection(http.client.HTTPSConnection):
    """
    `http.client.HTTPSConnection` subclass that pins its underlying socket to the
    physical Wi-Fi interface (`SO_BINDTODEVICE` and/or `source_address=(wifi_ip, 0)`).
    """

    def __init__(
        self,
        host: str,
        port: Optional[int] = None,
        timeout: float = 10.0,
        context: Optional[ssl.SSLContext] = None,
        wifi_iface: Optional[str] = None,
        wifi_ip: Optional[str] = None,
        **kwargs: object,
    ) -> None:
        self.wifi_iface = wifi_iface
        self.wifi_ip = wifi_ip
        is_loopback = host.startswith("127.") or host.lower() == "localhost"
        src_addr = (wifi_ip, 0) if (wifi_ip and not is_loopback and not wifi_ip.startswith("127.")) else None
        super().__init__(host, port=port, timeout=timeout, context=context, source_address=src_addr)

    def connect(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        bind_socket_to_wifi(
            sock,
            wifi_iface=self.wifi_iface,
            wifi_ip=self.wifi_ip,
            target_ip=self.host,
        )
        sock.connect((self.host, self.port))
        server_hostname = self._tunnel_host if self._tunnel_host else self.host
        self.sock = self._context.wrap_socket(sock, server_hostname=server_hostname)


def check_tv_reachable(
    tv_ip: str,
    tv_port: int = 52266,
    timeout: float = 0.5,
    wifi_info: Optional[WiFiInterfaceInfo] = None,
) -> bool:
    """
    Fast TCP connect reachability check (`0.5s` default timeout) to `<tv_ip>:<tv_port>`
    pinned to the physical Wi-Fi interface.
    """
    if not tv_ip or not (1 <= int(tv_port) <= 65535):
        return False

    is_loopback = tv_ip.startswith("127.") or tv_ip.lower() == "localhost"
    wifi = None if is_loopback else (wifi_info or find_physical_wifi_interface())

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if wifi:
            bind_socket_to_wifi(sock, wifi_iface=wifi.ifname, wifi_ip=wifi.ipv4, target_ip=tv_ip)
        sock.connect((tv_ip, int(tv_port)))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def expand_activation_url(
    raw_input: str,
    room_number: str = "",
    portal_base_url: str = DEFAULT_PORTAL_BASE_URL,
) -> str:
    """
    Expand a full portal URL (`https://...`), a `domain/path` string, or a short
    QR-portal token (optionally paired with `room_number`) into a full HTTP(S) URL.
    """
    cleaned = (raw_input or "").strip()
    room = (room_number or "").strip()
    if not cleaned:
        raise ValueError("Activation URL or token cannot be empty.")

    if cleaned.lower().startswith(("http://", "https://")):
        return cleaned

    first_segment = cleaned.split("/", 1)[0]
    if "." in first_segment:
        stripped = re.sub(r"^www\.", "", cleaned, flags=re.IGNORECASE).strip("/")
        parts = [p for p in stripped.split("/") if p]
        if len(parts) == 2 and parts[0].lower() == "ovk.top" and room:
            return f"https://ovk.top/{parts[1]}/{room}"
        return f"https://{cleaned.lstrip('/')}"

    base = (portal_base_url or DEFAULT_PORTAL_BASE_URL).rstrip("/")
    parts = [p for p in cleaned.strip("/").split("/") if p]
    if len(parts) == 1:
        return f"{base}/{parts[0]}/{room}" if room else f"{base}/{parts[0]}"
    if len(parts) >= 2:
        return f"{base}/{'/'.join(parts)}"
    return f"{base}/{cleaned}/{room}" if room else f"{base}/{cleaned}"


def activate_hotel_tv_bridge(
    url_or_token: str,
    room_number: str = "",
    timeout: float = 8.0,
    on_log: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str]:
    """
    Perform an HTTP GET request via `urllib.request` (following redirects with a standard
    browser User-Agent, `ssl._create_unverified_context()`, `ProxyHandler({})` to bypass
    any active HTTP/SOCKS proxies, and Wi-Fi socket pinning) to trigger hotel router
    TV-to-client IP binding.
    """
    full_url = expand_activation_url(url_or_token, room_number=room_number)
    wifi = find_physical_wifi_interface()

    if on_log:
        iface_desc = f" via {wifi.ifname} ({wifi.ipv4}, gw={wifi.gateway})" if wifi else ""
        on_log(f"Activating Hotel TV Bridge: GET {full_url}{iface_desc}...")

    ssl_ctx = ssl._create_unverified_context()

    class _WiFiHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(
                lambda host, **kw: WiFiPinnedHTTPConnection(
                    host,
                    wifi_iface=wifi.ifname if wifi else None,
                    wifi_ip=wifi.ipv4 if wifi else None,
                    **kw,
                ),
                req,
            )

    class _WiFiHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(
                lambda host, **kw: WiFiPinnedHTTPSConnection(
                    host,
                    context=ssl_ctx,
                    wifi_iface=wifi.ifname if wifi else None,
                    wifi_ip=wifi.ipv4 if wifi else None,
                    **kw,
                ),
                req,
            )

    redirect_chain: List[str] = []

    class _RedirectPinHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            redirect_chain.append(newurl)
            try:
                parsed = urllib.parse.urlparse(newurl)
                if parsed.hostname and re.match(r"^\d+\.\d+\.\d+\.\d+$", parsed.hostname):
                    ensure_vpn_bypass_route(parsed.hostname, wifi_info=wifi, on_log=on_log)
                if on_log:
                    on_log(f"Following portal redirect (HTTP {code}) -> {newurl}")
            except Exception:
                pass
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RedirectPinHandler(),
        _WiFiHTTPHandler(),
        _WiFiHTTPSHandler(context=ssl_ctx),
    )

    candidates = [full_url]
    m_tok = re.match(r"^(https?://ovk\.top/[^/]+)/[^/]+$", full_url, re.IGNORECASE)
    if m_tok and m_tok.group(1) not in candidates:
        candidates.append(m_tok.group(1))

    last_err_msg = ""
    for candidate_url in candidates:
        try:
            req = urllib.request.Request(
                candidate_url,
                headers={
                    "User-Agent": BROWSER_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
                method="GET",
            )
            try:
                with opener.open(req, timeout=timeout) as resp:
                    final_url = resp.geturl()
                    raw_bytes = resp.read(65536)
                    body_text = raw_bytes.decode("utf-8", errors="replace")
            except urllib.error.HTTPError as http_err:
                final_url = http_err.geturl()
                err_body = ""
                try:
                    err_body = http_err.read(65536).decode("utf-8", errors="replace")
                except Exception:
                    pass
                if "绑定电视成功" in err_body:
                    msg = f"TV Bridge Activated: 绑定电视成功 ({final_url})"
                    if on_log:
                        on_log(msg)
                    return True, msg
                snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", err_body)).strip()[:240]
                server_hdr = http_err.headers.get("Server", "unknown") if http_err.headers else "unknown"
                last_err_msg = (
                    f"HTTP {http_err.code} ({http_err.reason}) from {final_url} [Server: {server_hdr}]"
                    + (f" — Body: {snippet}" if snippet else "")
                )
                if on_log:
                    on_log(f"Portal response on {candidate_url}: {last_err_msg}")
                continue

            if "绑定电视成功" in body_text:
                msg = f"TV Bridge Activated: 绑定电视成功 ({final_url})"
                if on_log:
                    on_log(msg)
                return True, msg

            snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body_text)).strip()[:200]
            msg = f"Portal responded (HTTP 200) from {final_url}: {snippet or 'OK'}"
            if on_log:
                on_log(msg)
            return True, msg

        except Exception as exc:
            last_err_msg = f"Hotel portal activation failed for {candidate_url}: {exc}"
            if on_log:
                on_log(f"WARNING: {last_err_msg}")

    return False, last_err_msg
