"""Network radar: discover and identify devices on the local network.

Only private (RFC 1918) networks that this machine is directly attached to are ever scanned.
Discovery = ping/TCP sweep -> ARP/neighbour table (MAC) -> reverse DNS / NetBIOS / mDNS / SSDP names ->
vendor from the MAC's OUI -> a small port probe -> heuristic device classification.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psutil

DATA = Path(__file__).parent / "data" / "oui.json"
PROBE_PORTS = {22: "ssh", 23: "telnet", 53: "dns", 80: "http", 443: "https", 445: "smb", 554: "rtsp", 631: "ipp", 1900: "upnp", 3389: "rdp",
               5000: "http-alt", 5353: "mdns", 7000: "airplay", 8008: "chromecast", 8009: "chromecast", 8080: "http-alt", 9100: "printer", 62078: "iphone-sync"}
MAX_HOSTS = 1024


# -- local networks ----------------------------------------------------------
def local_networks() -> list[dict[str, Any]]:
    nets = []
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family != socket.AF_INET or not a.netmask:
                continue
            try:
                iface = ipaddress.ip_interface(f"{a.address}/{a.netmask}")
            except ValueError:
                continue
            if iface.ip.is_loopback or iface.ip.is_link_local or not iface.ip.is_private:
                continue
            net = iface.network
            if net.prefixlen < 22:  # never sweep anything bigger than a /22 by default
                net = ipaddress.ip_network(f"{iface.ip}/22", strict=False)
            nets.append({"iface": name, "ip": str(iface.ip), "network": str(net)})
    return nets


def assert_scannable(cidr: str) -> ipaddress.IPv4Network:
    """Refuse anything that is not a private network attached to this machine."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError as e:
        raise ValueError(f"Bad network {cidr!r}: {e}")
    if net.version != 4 or not net.is_private or net.is_loopback:
        raise ValueError(f"Refusing to scan {net}: only private IPv4 networks on this machine's own interfaces are allowed.")
    if net.num_addresses > MAX_HOSTS:
        raise ValueError(f"{net} is too large (max {MAX_HOSTS} addresses).")
    attached = [ipaddress.ip_network(n["network"]) for n in local_networks()]
    if not any(net.subnet_of(a) for a in attached):
        raise ValueError(f"{net} is not a network this machine is connected to (attached: {[str(a) for a in attached] or 'none'}).")
    return net  # type: ignore[return-value]


# -- MAC / vendor ------------------------------------------------------------
_oui: dict[str, str] | None = None


def load_oui() -> dict[str, str]:
    global _oui
    if _oui is None:
        try:
            _oui = json.loads(DATA.read_text())
        except (OSError, ValueError):
            _oui = {}
    return _oui


def normalize_mac(mac: str) -> str:
    parts = re.split(r"[:\-.]", mac.strip().lower())
    if len(parts) == 1 and len(parts[0]) == 12:
        parts = [parts[0][k:k + 2] for k in range(0, 12, 2)]
    return ":".join(p.zfill(2) for p in parts)


def is_randomized(mac: str) -> bool:
    """Locally-administered bit set: phones/laptops use these 'private' addresses for Wi-Fi privacy."""
    return bool(int(mac.split(":")[0], 16) & 0b10)


def vendor_of(mac: str) -> str | None:
    if is_randomized(mac):
        return None
    return load_oui().get(mac.replace(":", "")[:6].upper())


# -- ARP table ---------------------------------------------------------------
def parse_arp(text: str) -> dict[str, str]:
    """Parse `arp -an`, `ip neigh`, or /proc/net/arp output into {ip: mac}."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        ip = re.search(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", line)
        mac = re.search(r"(?<![0-9a-fA-F:-])((?:[0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2})(?![0-9a-fA-F:-])", line)
        if ip and mac:
            m = normalize_mac(mac.group(1))
            if m not in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
                out[ip.group(1)] = m
    return out


def read_arp() -> dict[str, str]:
    if sys.platform.startswith("linux") and os.path.exists("/proc/net/arp"):
        try:
            table = parse_arp(Path("/proc/net/arp").read_text())
            if table:
                return table
        except OSError:
            pass
    for cmd in (["ip", "neigh"], ["arp", "-an"]):
        if shutil.which(cmd[0]):
            try:
                return parse_arp(subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout)
            except (OSError, subprocess.SubprocessError):
                continue
    return {}


# -- probes ------------------------------------------------------------------
async def _ping(ip: str, sem: asyncio.Semaphore) -> bool:
    flag = ["-n", "1", "-w", "800"] if os.name == "nt" else (["-c", "1", "-W", "1"] if sys.platform != "darwin" else ["-c", "1", "-W", "800"])
    if not shutil.which("ping"):
        return False
    async with sem:
        proc = await asyncio.create_subprocess_exec("ping", *flag, ip, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            return (await asyncio.wait_for(proc.wait(), 3)) == 0
        except asyncio.TimeoutError:
            proc.kill()
            return False


async def _tcp_poke(ip: str, sem: asyncio.Semaphore, ports=(80, 443, 445, 22, 62078)) -> bool:
    """A connect attempt makes the host answer ARP even when it drops ICMP. Any answer (open or refused) = alive."""
    async with sem:
        for port in ports:
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), 0.35)
                w.close()
                return True
            except ConnectionRefusedError:
                return True
            except (asyncio.TimeoutError, OSError):
                continue
    return False


async def _open_ports(ip: str, sem: asyncio.Semaphore) -> list[int]:
    async def one(port: int) -> int | None:
        async with sem:
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), 0.6)
                w.close()
                return port
            except (asyncio.TimeoutError, OSError):
                return None
    return sorted(p for p in await asyncio.gather(*(one(p) for p in PROBE_PORTS)) if p)


async def _rdns(ip: str) -> str:
    try:
        return (await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, socket.gethostbyaddr, ip), 1.5))[0]
    except (asyncio.TimeoutError, OSError):
        return ""


class _Collector(asyncio.DatagramProtocol):
    """Collects UDP replies. Works on every event loop (uvloop lacks loop.sock_recvfrom)."""

    def __init__(self) -> None:
        self.packets: list[tuple[bytes, tuple]] = []
        self.first = asyncio.Event()

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.packets.append((data, addr))
        self.first.set()


async def _udp_exchange(payload: bytes, dest: tuple[str, int], timeout: float, multicast: bool = False, until_first: bool = False) -> list[tuple[bytes, tuple]]:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setblocking(False)
        if multicast:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 2))
        transport, proto = await loop.create_datagram_endpoint(_Collector, sock=sock)
    except OSError:
        sock.close()
        return []
    try:
        transport.sendto(payload, dest)
        if until_first:
            try:
                await asyncio.wait_for(proto.first.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(timeout)
        return list(proto.packets)
    except OSError:
        return []
    finally:
        transport.close()


async def _netbios(ip: str) -> str:
    """NBNS node-status query (Windows/Samba machines answer with their name)."""
    pkt = b"\x82\x28\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00\x20CKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\x00\x00\x21\x00\x01"
    for data, _ in await _udp_exchange(pkt, (ip, 137), 0.8, until_first=True):
        if len(data) > 57:
            return data[57:72].decode("ascii", "ignore").strip()
    return ""


async def ssdp_discover(timeout: float = 2.5) -> dict[str, dict[str, str]]:
    """UPnP M-SEARCH: smart TVs, routers, consoles, media servers announce themselves."""
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n").encode()
    found: dict[str, dict[str, str]] = {}
    for data, addr in await _udp_exchange(msg, ("239.255.255.250", 1900), timeout, multicast=True):
        hdrs = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in data.decode("utf-8", "ignore").split("\r\n")[1:]) if k}
        found.setdefault(addr[0], {}).update({k: hdrs[k] for k in ("server", "st", "usn") if k in hdrs})
    return found


async def mdns_discover(timeout: float = 3.0) -> dict[str, dict[str, Any]]:
    """Bonjour/mDNS names and services via zeroconf, if installed (pip install 'mrx[mdns]')."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf  # noqa: PLC0415
    except ImportError:
        return {}
    found: dict[str, dict[str, Any]] = {}

    def run() -> None:
        zc = Zeroconf()
        try:
            types = ["_airplay._tcp.local.", "_googlecast._tcp.local.", "_ipp._tcp.local.", "_printer._tcp.local.", "_hap._tcp.local.", "_spotify-connect._tcp.local.",
                     "_smb._tcp.local.", "_ssh._tcp.local.", "_http._tcp.local.", "_companion-link._tcp.local.", "_raop._tcp.local.", "_homekit._tcp.local."]

            class L:
                def add_service(self, zc_, t, name):
                    info = zc_.get_service_info(t, name, 1500)
                    if info:
                        for addr in info.parsed_addresses():
                            e = found.setdefault(addr, {"name": info.server.rstrip(".").removesuffix(".local"), "services": []})
                            e["services"].append(t.split(".")[0].lstrip("_"))
                            if info.properties.get(b"fn") or info.properties.get(b"md"):
                                e["model"] = (info.properties.get(b"md") or info.properties.get(b"fn") or b"").decode("utf-8", "ignore")

                def update_service(self, *a): ...
                def remove_service(self, *a): ...
            browsers = [ServiceBrowser(zc, t, L()) for t in types]
            time.sleep(timeout)
            for br in browsers:
                br.cancel()
        finally:
            zc.close()

    await asyncio.get_running_loop().run_in_executor(None, run)
    return found


# -- classification ----------------------------------------------------------
def classify(dev: dict[str, Any]) -> tuple[str, str]:
    """Best-guess (kind, confidence) from vendor, hostname, open ports, and announced services."""
    vendor = (dev.get("vendor") or "").lower()
    host = (dev.get("hostname") or "").lower()
    ports = set(dev.get("open_ports") or [])
    svcs = set(dev.get("services") or [])
    ssdp = (dev.get("ssdp") or {}).get("server", "").lower()
    blob = f"{vendor} {host} {ssdp}"

    def has(*words: str) -> bool:
        return any(w in blob for w in words)

    if dev.get("is_gateway"):
        return "router", "high"
    if 9100 in ports or 631 in ports or svcs & {"ipp", "printer"} or has("printer", "epson", "brother", "canon", "lexmark", "laserjet"):
        return "printer", "high" if (9100 in ports or 631 in ports or svcs & {"ipp", "printer"}) else "medium"
    if 554 in ports or has("hikvision", "dahua", "reolink", "camera", "ipcam", "axis comm", "wyze"):
        return "camera", "medium"
    if 62078 in ports or has("iphone", "ipad", "ipod"):
        return "phone/tablet (Apple)", "high"
    if svcs & {"googlecast"} or ports & {8008, 8009} or has("chromecast", "google-home", "nest"):
        return "streaming/smart speaker (Google)", "medium"
    if svcs & {"airplay", "raop"} or 7000 in ports:
        return ("Apple TV/HomePod/Mac" if has("apple") else "AirPlay device"), "medium"
    if has("roku", "samsung", "lg elec", "lge", "sony", "vizio", "tcl", "hisense", "webos", "tizen", "smarttv", "bravia") and ("upnp" in blob or 1900 in ports or "tv" in blob or ssdp):
        return "smart TV", "medium"
    if has("playstation", "xbox", "nintendo"):
        return "game console", "medium"
    if has("amazon", "echo", "alexa", "kindle", "fire"):
        return "Amazon device (Echo/Fire)", "medium"
    if has("espressif", "tuya", "shelly", "sonoff", "tp-link", "tplink", "philips lighting", "signify", "ikea", "xiaomi", "yeelight", "wiz", "smartthings"):
        return "smart-home / IoT", "medium"
    if has("raspberry"):
        return "Raspberry Pi", "high"
    if ports & {3389, 445} and has("microsoft", "dell", "lenovo", "hp", "intel", "asus", "acer", "msi", "windows", "desktop", "laptop"):
        return "Windows PC", "medium"
    if 22 in ports and not ports & {80, 443, 3389}:
        return "Linux/Unix host", "low"
    if has("apple") or svcs & {"companion-link"}:
        return "Apple device (Mac/iPhone/iPad)", "medium"
    if has("samsung", "huawei", "oppo", "oneplus", "xiaomi", "vivo", "motorola", "realme", "pixel", "android"):
        return "phone/tablet (Android)", "medium"
    if dev.get("randomized_mac"):
        return "phone/laptop (private Wi-Fi address)", "low"
    if has("ubiquiti", "netgear", "d-link", "asustek", "zyxel", "mikrotik", "cisco", "aruba", "huawei tech") and ports & {80, 443, 22}:
        return "network equipment", "medium"
    return "unknown", "low"


def default_gateway() -> str | None:
    try:
        if sys.platform.startswith("linux") and os.path.exists("/proc/net/route"):
            for line in Path("/proc/net/route").read_text().splitlines()[1:]:
                f = line.split()
                if f[1] == "00000000" and int(f[3], 16) & 2:
                    return socket.inet_ntoa(struct.pack("<L", int(f[2], 16)))
        cmd = ["route", "-n", "get", "default"] if sys.platform == "darwin" else (["ipconfig"] if os.name == "nt" else None)
        if cmd and shutil.which(cmd[0]):
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"gateway:\s*(\S+)", out) or re.search(r"Default Gateway[ .]*:\s*(\d+\.\d+\.\d+\.\d+)", out)
            return m.group(1) if m else None
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


# -- the scan ----------------------------------------------------------------
async def scan(cidr: str | None = None, deep: bool = True, on_progress=None) -> dict[str, Any]:
    """Scan one attached private network. Returns {network, devices:[...], took_s}."""
    started = time.time()
    nets = local_networks()
    if not nets:
        raise ValueError("No private network interface found — are you connected to Wi-Fi/Ethernet?")
    net = assert_scannable(cidr or nets[0]["network"])
    my_ips = {n["ip"] for n in nets}
    self_macs = {}
    for ifname, addrs in psutil.net_if_addrs().items():
        ipv4 = [a.address for a in addrs if a.family == socket.AF_INET]
        link = [a.address for a in addrs if a.family == psutil.AF_LINK]
        if link and link[0]:
            self_macs.update({ip: normalize_mac(link[0]) for ip in ipv4})
    hosts = [str(h) for h in net.hosts()]
    sem = asyncio.Semaphore(128)

    async def progress(stage: str, pct: int) -> None:
        if on_progress:
            await on_progress({"stage": stage, "pct": pct})

    await progress("Sweeping addresses", 5)
    ssdp_task = asyncio.create_task(ssdp_discover())
    mdns_task = asyncio.create_task(mdns_discover()) if deep else None
    await asyncio.gather(*(asyncio.gather(_ping(h, sem), _tcp_poke(h, sem)) for h in hosts))
    await progress("Reading ARP table", 45)
    arp = {ip: mac for ip, mac in read_arp().items() if ipaddress.ip_address(ip) in net}
    ssdp = await ssdp_task
    mdns = await mdns_task if mdns_task else {}
    gw = default_gateway()
    live_ips = sorted(set(arp) | (set(ssdp) & set(hosts)), key=lambda x: ipaddress.ip_address(x))

    await progress("Identifying devices", 60)
    devices: list[dict[str, Any]] = []

    async def identify(ip: str) -> dict[str, Any]:
        mac = arp.get(ip) or self_macs.get(ip) or ""
        dev: dict[str, Any] = {"ip": ip, "mac": mac or f"unknown-{ip}", "is_self": ip in my_ips, "is_gateway": ip == gw}
        dev["randomized_mac"] = bool(mac) and is_randomized(mac)
        dev["vendor"] = vendor_of(mac) if mac else None
        names = await asyncio.gather(_rdns(ip), _netbios(ip)) if deep else ("", "")
        dev["hostname"] = mdns.get(ip, {}).get("name") or names[0] or names[1]
        dev["services"] = mdns.get(ip, {}).get("services", [])
        if mdns.get(ip, {}).get("model"):
            dev["model"] = mdns[ip]["model"]
        dev["ssdp"] = ssdp.get(ip, {})
        dev["open_ports"] = await _open_ports(ip, sem) if deep else []
        dev["open_services"] = [PROBE_PORTS[p] for p in dev["open_ports"]]
        dev["kind"], dev["confidence"] = classify(dev)
        if dev["is_self"]:
            dev["kind"], dev["confidence"], dev["hostname"] = "this computer (M.R.X.)", "high", dev["hostname"] or socket.gethostname()
        return dev

    own = {ip for ip in my_ips if ipaddress.ip_address(ip) in net}
    live_ips = sorted(set(live_ips) | own, key=lambda x: ipaddress.ip_address(x))
    devices = list(await asyncio.gather(*(identify(ip) for ip in live_ips)))
    await progress("Done", 100)
    return {"network": str(net), "gateway": gw, "devices": devices, "took_s": round(time.time() - started, 1), "time": time.time()}
