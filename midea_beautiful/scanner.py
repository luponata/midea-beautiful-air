"""Scans local network for Midea appliances."""
from __future__ import annotations

from ipaddress import IPv4Network
import logging
import socket
from typing import Final

from ifaddr import IP, Adapter, get_adapters

from midea_beautiful.appliance import Appliance
from midea_beautiful.cloud import MideaCloud
from midea_beautiful.exceptions import MideaError
from midea_beautiful.lan import DISCOVERY_MSG, LanDevice, matches_lan_cloud
from midea_beautiful.midea import DISCOVERY_PORT
from midea_beautiful.util import SPAM, TRACE
from midea_beautiful.version import __version__

_LOGGER = logging.getLogger(__name__)

_BROADCAST_TIMEOUT: Final = 3
_BROADCAST_RETRIES: Final = 3


def _get_broadcast_addresses(addresses: list[str] = []) -> list[str]:
    """Retrieves local networks by iterating local network adapters

    Returns:
        list[str]: list of local network broadcast addresses
    """
    # If addresses were provided, then we will send discovery to them
    # even if they are not in private ip range
    nets: list[IPv4Network] = []
    for addr in addresses:
        localNet = IPv4Network(addr, strict=False)
        if not localNet.is_loopback and not localNet.is_link_local:
            nets.append(localNet)

    if not addresses:
        adapters: list[Adapter] = get_adapters()
        for adapter in adapters:
            ip: IP
            for ip in adapter.ips:

                if ip.is_IPv4:
                    addr = f"{ip.ip}/{ip.network_prefix}"
                    localNet = IPv4Network(addr, strict=False)
                    if (
                        localNet.is_private
                        and not localNet.is_loopback
                        and not localNet.is_link_local
                    ):
                        nets.append(localNet)
    networks = list()
    if not nets:
        raise MideaError("No valid networks to send broadcast to")
    else:
        for net in nets:
            _LOGGER.debug(
                "Network %s, broadcast address %s",
                net.network_address,
                net.broadcast_address,
            )
            networks.append(str(net.broadcast_address))
    return networks


class MideaDiscovery:
    def __init__(self, cloud: MideaCloud | None) -> None:
        self._cloud = cloud
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._socket.settimeout(_BROADCAST_TIMEOUT)
        self._known_ips = set()
        self._networks: list[str] = []

    def collect_appliances(self, networks: list[str] = []) -> list[LanDevice]:
        """Find all appliances on the local network."""

        self._broadcast_message(networks)

        scanned_appliances: set[LanDevice] = set()
        try:
            while True:
                data, addr = self._socket.recvfrom(512)
                ip = addr[0]
                if ip not in self._known_ips:
                    _LOGGER.log(TRACE, "Reply from ip=%s payload=%s", ip, data)
                    self._known_ips.add(ip)
                    appliance = LanDevice(data=data)
                    if appliance.is_supported:
                        scanned_appliances.add(appliance)
                    else:
                        _LOGGER.debug("Not supported appliance %s", appliance)

        except socket.timeout:
            # If we got timeout, it was enough time to wait for broadcast response
            _LOGGER.debug("Finished broadcast collection")

        # Return only successfully identified appliances
        return [sd for sd in scanned_appliances if sd.is_identified(self._cloud)]

    def _broadcast_message(self, networks: list[str]) -> None:

        for addr in networks:
            _LOGGER.debug("Broadcasting to %s", addr)
            try:
                _LOGGER.log(
                    SPAM, "UDP broadcast %s:%d %s", addr, DISCOVERY_PORT, DISCOVERY_MSG
                )
                self._socket.sendto(DISCOVERY_MSG, (addr, DISCOVERY_PORT))
            except Exception as ex:
                _LOGGER.debug("Unable to send broadcast to: %s cause %s", addr, ex)


def _add_missing_appliances(
    cloud_appliances: list[dict], appliances: list[LanDevice], count: int
) -> None:
    """
    Utility method to add placeholders for appliances which were not
    discovered on local network
    """
    _LOGGER.warning(
        (
            "Some appliance(s) where not discovered on local network(s):"
            " %d discovered out of %d"
        ),
        len(appliances),
        count,
    )
    for known in cloud_appliances:
        if Appliance.supported(known["type"]):
            for local in appliances:
                if matches_lan_cloud(local, known):
                    break
            else:
                local = LanDevice(id=known["id"], appliance_type=known["type"])
                appliances.append(local)
                _LOGGER.warning(
                    "Unable to discover registered appliance %s",
                    known,
                )
            if local.sn is None:
                local.sn = known["sn"]
            local.name = known["name"]


def _find_appliances_on_lan(
    cloud: MideaCloud | None, networks: list[str]
) -> list[LanDevice]:

    discovery = MideaDiscovery(cloud=cloud)
    appliances: list[LanDevice] = []
    _LOGGER.debug("Starting LAN discovery")
    cloud_appliances = cloud.list_appliances() if cloud else []
    count = sum(Appliance.supported(a["type"]) for a in cloud_appliances)
    known_cloud_appliances = set(a["id"] for a in cloud_appliances)
    for i in range(_BROADCAST_RETRIES):
        _LOGGER.debug("Broadcast attempt %d of max %d", i + 1, _BROADCAST_RETRIES)

        scanned_appliances = list(discovery.collect_appliances(networks))
        scanned_appliances.sort(key=lambda appliance: appliance.id)
        for scanned in scanned_appliances:
            for appliance in appliances:
                if appliance.id == scanned.id:
                    _LOGGER.debug("Known appliance %s", scanned.id)
                    if appliance.ip != scanned.ip:
                        # Already known
                        appliance.update(scanned)
                    break

            for details in cloud_appliances:
                if matches_lan_cloud(scanned, details):
                    scanned.name = details["name"]
                    appliances.append(scanned)
                    _LOGGER.info("Found appliance %s", scanned)
                    known_cloud_appliances.remove(details["id"])
                    break
            else:
                _LOGGER.warning(
                    "Found an appliance that is not registered to the account: %s",
                    scanned,
                )
        if len(known_cloud_appliances) == 0:
            break
    _LOGGER.info("Found %d of %d appliance(s)", len(appliances), count)
    if len(appliances) < count:
        _add_missing_appliances(cloud_appliances, appliances, count)
    return appliances


def find_appliances(
    cloud: MideaCloud | None = None,
    appkey: str | None = None,
    account: str = None,
    password: str = None,
    appid: str = None,
    networks: list[str] = [],
) -> list[LanDevice]:
    _LOGGER.debug("Library version=%s", __version__)
    if not cloud and account and password:
        cloud = MideaCloud(appkey, account, password, appid)
        cloud.authenticate()

    addresses = _get_broadcast_addresses(networks or [])
    _LOGGER.debug("Scanning for midea dehumidifier appliances via %s", addresses)
    return _find_appliances_on_lan(cloud, addresses)
