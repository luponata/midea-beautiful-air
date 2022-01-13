"""Scans network for Midea appliances."""
from __future__ import annotations

import logging
import socket
from typing import Final

from midea_beautiful.appliance import Appliance
from midea_beautiful.cloud import MideaCloud
from midea_beautiful.lan import DISCOVERY_MSG, LanDevice, matches_lan_cloud
from midea_beautiful.midea import DISCOVERY_PORT
from midea_beautiful.util import SPAM, TRACE

# pylint: disable=too-few-public-methods
# pylint: disable=too-many-arguments

_LOGGER = logging.getLogger(__name__)

_BROADCAST_TIMEOUT: Final = 3
_BROADCAST_RETRIES: Final = 3


class _MideaDiscovery:
    """Utility class to discover appliances on local network"""

    def __init__(
        self, cloud: MideaCloud | None, max_retries: int = _BROADCAST_RETRIES
    ) -> None:
        self._cloud = cloud
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._socket.settimeout(_BROADCAST_TIMEOUT)
        self._known_ips = set()
        self._max_retries = max_retries

    def _collect_appliances(self, addresses: list[str] = None) -> list[LanDevice]:
        """Find all appliances on the local network."""
        addresses = addresses or []

        self._broadcast_message(addresses)

        scanned_appliances: set[LanDevice] = set()
        try:
            while True:
                data, addr = self._socket.recvfrom(512)
                ip_address = addr[0]
                if ip_address not in self._known_ips:
                    _LOGGER.log(
                        TRACE, "Reply from address=%s payload=%s", ip_address, data
                    )
                    self._known_ips.add(ip_address)
                    appliance = LanDevice(data=data)
                    if Appliance.supported(appliance.type):
                        scanned_appliances.add(appliance)
                    else:
                        _LOGGER.debug("Not supported appliance %s", appliance)

        except socket.timeout:
            # If we got timeout, it was enough time to wait for broadcast response
            _LOGGER.debug("Finished broadcast collection")

        # Return only successfully identified appliances
        if not self._cloud:
            return list(scanned_appliances)
        return [sd for sd in scanned_appliances if sd.is_identified(self._cloud)]

    def _broadcast_message(self, addresses: list[str]) -> None:

        for addr in addresses:
            _LOGGER.debug("Broadcasting to %s", addr)
            try:
                _LOGGER.log(
                    SPAM, "UDP broadcast %s:%d %s", addr, DISCOVERY_PORT, DISCOVERY_MSG
                )
                self._socket.sendto(DISCOVERY_MSG, (addr, DISCOVERY_PORT))
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.debug("Unable to send broadcast to: %s cause %s", addr, ex)

    def broadcast(
        self,
        count,
        addresses: list[str],
        appliances: list[LanDevice],
        cloud_appliances: list[dict],
        known_cloud_appliances: set[str],
        discover_all: bool,
    ):
        """Broadcasts and discovers appliances on the networks"""
        _LOGGER.debug(
            "Broadcast attempt %d of max %d",
            count + 1,
            self._max_retries,
        )

        scanned_appliances = list(self._collect_appliances(addresses))

        scanned_appliances.sort(key=lambda appliance: appliance.appliance_id)
        for scanned in scanned_appliances:
            for appliance in appliances:
                if appliance.appliance_id == scanned.appliance_id:
                    if appliance.address != scanned.address:
                        _LOGGER.debug(
                            "Known appliance %s, data changed %s", appliance, scanned
                        )
                        appliance.update(scanned)
                    break
            else:
                if not discover_all:
                    self._match_with_cloud(
                        appliances, cloud_appliances, known_cloud_appliances, scanned
                    )
                else:
                    appliances.append(scanned)

    def _match_with_cloud(
        self, appliances, cloud_appliances, known_cloud_appliances, scanned
    ):
        for details in cloud_appliances:
            if matches_lan_cloud(scanned, details):
                scanned.name = details["name"]
                appliances.append(scanned)
                _LOGGER.debug("Found appliance %s %s", scanned, known_cloud_appliances)
                if details["id"] in known_cloud_appliances:
                    known_cloud_appliances.remove(details["id"])
                break
        else:
            _LOGGER.warning(
                "Found an appliance that is not registered to the account: %s",
                scanned,
            )


def _add_missing_appliances(
    cloud_appliances: list[dict], appliances: list[LanDevice], count: int
) -> None:
    """
    Utility method to add placeholders for appliances which were not
    discovered on local network
    """
    _LOGGER.warning(
        (
            "Some appliance(s) where not discovered on local network:"
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
                local = LanDevice(
                    appliance_id=known["id"],
                    appliance_type=known["type"],
                    serial_number=known["sn"],
                )
                appliances.append(local)
                _LOGGER.warning(
                    "Unable to discover registered appliance %s",
                    known,
                )
            local.name = known["name"]


def do_find_appliances(
    cloud: MideaCloud | None,
    addresses: list[str],
    appliances: list[LanDevice] = None,
    max_retries=_BROADCAST_RETRIES,
) -> list[LanDevice]:

    discovery = _MideaDiscovery(cloud=cloud, max_retries=max_retries)
    appliances = appliances or []
    _LOGGER.debug("Starting LAN discovery")
    if cloud:
        discover_all = False
        cloud_appliances = cloud.list_appliances()
        cloud_count = sum(Appliance.supported(a["type"]) for a in cloud_appliances)
        known_cloud_appliances = set(a["id"] for a in cloud_appliances)
    else:
        discover_all = True
        cloud_appliances = []
        cloud_count = 0
        known_cloud_appliances = set()
    for i in range(max_retries):
        discovery.broadcast(
            count=i,
            addresses=addresses,
            appliances=appliances,
            cloud_appliances=cloud_appliances,
            known_cloud_appliances=known_cloud_appliances,
            discover_all=discover_all,
        )
        if cloud and len(known_cloud_appliances) == 0:
            break
    if cloud:
        _LOGGER.debug("Found %d of %d appliance(s)", len(appliances), cloud_count)
    else:
        _LOGGER.debug("Found %d appliance(s)", len(appliances))

    if cloud and len(appliances) < cloud_count:
        _add_missing_appliances(cloud_appliances, appliances, cloud_count)
    return appliances
