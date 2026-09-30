import asyncio
import ipaddress

import pytest

from mrx import radar

ARP_PROC = """IP address       HW type     Flags       HW address            Mask     Device
192.168.1.1      0x1         0x2         AA:BB:CC:00:11:22     *        wlan0
192.168.1.20     0x1         0x0         00:00:00:00:00:00     *        wlan0
192.168.1.33     0x1         0x2         3c:5a:b4:01:02:03     *        wlan0
"""
ARP_AN = "? (192.168.1.44) at 0:1a:2b:3:4:5 on en0 ifscope [ethernet]\n? (192.168.1.255) at ff:ff:ff:ff:ff:ff on en0"
IP_NEIGH = "192.168.1.9 dev eth0 lladdr b8:27:eb:11:22:33 REACHABLE\n192.168.1.10 dev eth0  FAILED"


def test_parse_arp_formats():
    p = radar.parse_arp(ARP_PROC)
    assert p == {"192.168.1.1": "aa:bb:cc:00:11:22", "192.168.1.33": "3c:5a:b4:01:02:03"}   # incomplete entry dropped
    assert radar.parse_arp(ARP_AN) == {"192.168.1.44": "00:1a:2b:03:04:05"}                 # macOS short octets padded, broadcast dropped
    assert radar.parse_arp(IP_NEIGH) == {"192.168.1.9": "b8:27:eb:11:22:33"}


def test_mac_helpers():
    assert radar.normalize_mac("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert radar.normalize_mac("aabbccddeeff") == "aa:bb:cc:dd:ee:ff"
    assert radar.is_randomized("da:a1:19:00:00:01")        # locally administered bit
    assert not radar.is_randomized("b8:27:eb:00:00:01")


def test_oui_database_lookup():
    assert "raspberry" in (radar.vendor_of("b8:27:eb:11:22:33") or "").lower()
    assert radar.vendor_of("da:a1:19:00:00:01") is None      # randomized MACs have no vendor
    assert len(radar.load_oui()) > 20000


def _fake_local(monkeypatch, nets):
    monkeypatch.setattr(radar, "local_networks", lambda: [{"iface": "eth0", "ip": n.split("/")[0].rsplit(".", 1)[0] + ".5", "network": n} for n in nets])


def test_scan_guard_allows_only_attached_private_networks(monkeypatch):
    _fake_local(monkeypatch, ["192.168.1.0/24"])
    assert radar.assert_scannable("192.168.1.0/24")
    assert radar.assert_scannable("192.168.1.0/25")                 # subnet of an attached net
    for bad in ("8.8.8.0/24", "192.168.2.0/24", "10.0.0.0/8", "127.0.0.0/24", "0.0.0.0/0", "192.168.0.0/16", "not-a-network"):
        with pytest.raises(ValueError):
            radar.assert_scannable(bad)


def test_local_networks_skips_loopback_and_public():
    for n in radar.local_networks():
        net = ipaddress.ip_network(n["network"])
        assert net.is_private and not net.is_loopback


@pytest.mark.parametrize("dev,kind", [
    ({"is_gateway": True}, "router"),
    ({"open_ports": [9100]}, "printer"),
    ({"open_ports": [62078], "vendor": "Apple, Inc."}, "phone/tablet (Apple)"),
    ({"vendor": "Raspberry Pi Foundation", "open_ports": [22]}, "Raspberry Pi"),
    ({"services": ["googlecast"]}, "streaming/smart speaker (Google)"),
    ({"vendor": "Espressif Inc."}, "smart-home / IoT"),
    ({"randomized_mac": True}, "phone/laptop (private Wi-Fi address)"),
    ({}, "unknown"),
])
def test_classify(dev, kind):
    assert radar.classify(dev)[0] == kind


async def test_scan_pipeline_with_stubbed_network(monkeypatch):
    _fake_local(monkeypatch, ["192.168.1.0/24"])
    monkeypatch.setattr(radar, "read_arp", lambda: {"192.168.1.1": "b8:27:eb:00:00:01", "192.168.1.77": "da:a1:19:00:00:02", "10.9.9.9": "aa:aa:aa:aa:aa:aa"})
    monkeypatch.setattr(radar, "default_gateway", lambda: "192.168.1.1")

    async def quiet(*a, **k):
        return False

    async def no_ssdp(*a, **k):
        return {"192.168.1.77": {"server": "Linux UPnP/1.0 Samsung TV"}}
    async def no_mdns(*a, **k):
        return {}
    async def no_str(*a, **k):
        return ""
    async def ports(ip, sem):
        return [9100] if ip.endswith(".1") else []
    monkeypatch.setattr(radar, "_ping", quiet)
    monkeypatch.setattr(radar, "_tcp_poke", quiet)
    monkeypatch.setattr(radar, "ssdp_discover", no_ssdp)
    monkeypatch.setattr(radar, "mdns_discover", no_mdns)
    monkeypatch.setattr(radar, "_rdns", no_str)
    monkeypatch.setattr(radar, "_netbios", no_str)
    monkeypatch.setattr(radar, "_open_ports", ports)
    res = await radar.scan()
    by_ip = {d["ip"]: d for d in res["devices"]}
    assert "10.9.9.9" not in by_ip                        # outside the scanned network
    assert by_ip["192.168.1.1"]["kind"] == "router"
    assert by_ip["192.168.1.77"]["randomized_mac"] and by_ip["192.168.1.77"]["vendor"] is None
    assert "192.168.1.5" in by_ip and by_ip["192.168.1.5"]["is_self"]     # this machine is listed too


async def test_udp_exchange_roundtrip():
    class Echo(asyncio.DatagramProtocol):
        def connection_made(self, t):
            self.t = t

        def datagram_received(self, d, a):
            self.t.sendto(b"reply:" + d, a)

    tr, _ = await asyncio.get_running_loop().create_datagram_endpoint(Echo, local_addr=("127.0.0.1", 0))
    try:
        got = await radar._udp_exchange(b"ping", ("127.0.0.1", tr.get_extra_info("sockname")[1]), 1.0, until_first=True)
    finally:
        tr.close()
    assert got and got[0][0] == b"reply:ping"


def test_radar_probes_work_on_uvloop():
    """uvicorn[standard] runs on uvloop, which lacks loop.sock_recvfrom - the probes must not depend on it."""
    uvloop = pytest.importorskip("uvloop")

    async def go():
        return await radar.ssdp_discover(0.3), await radar._netbios("127.0.0.1")

    ssdp, nb = uvloop.run(go())
    assert isinstance(ssdp, dict) and nb == ""


async def test_run_scan_baseline_then_flags_new_devices(monkeypatch, memory):
    from mrx.tools.radar import run_scan

    def fake_result(macs):
        devs = [{"ip": f"192.168.1.{i + 2}", "mac": m, "vendor": "V", "hostname": "", "kind": "unknown", "confidence": "low", "open_ports": [], "is_self": i == 0}
                for i, m in enumerate(macs)]
        return {"network": "192.168.1.0/24", "gateway": None, "devices": devs, "took_s": 0.1, "time": 0}

    scans = [fake_result(["aa:aa:aa:00:00:01", "aa:aa:aa:00:00:02"]), fake_result(["aa:aa:aa:00:00:01", "aa:aa:aa:00:00:02", "aa:aa:aa:00:00:03"]),
             fake_result(["aa:aa:aa:00:00:01", "aa:aa:aa:00:00:02", "aa:aa:aa:00:00:03"])]

    async def fake_scan(cidr=None, deep=True, on_progress=None):
        return scans.pop(0)
    monkeypatch.setattr(radar, "scan", fake_scan)
    events = []

    async def emit(e):
        events.append(e)
    first = await run_scan(memory, emit)
    assert first["baseline"] and first["intruder_alerts"] == []          # first scan = baseline, no alarms
    second = await run_scan(memory, emit)
    assert [d["mac"] for d in second["intruder_alerts"]] == ["aa:aa:aa:00:00:03"]
    memory.label_device("aa:aa:aa:00:00:03", "Guest laptop", True)
    third = await run_scan(memory, emit)
    assert third["intruder_alerts"] == []
    assert [d["label"] for d in third["devices"] if d["mac"].endswith("03")] == ["Guest laptop"]
    assert all(d["trusted"] for d in third["devices"] if d["is_self"])
