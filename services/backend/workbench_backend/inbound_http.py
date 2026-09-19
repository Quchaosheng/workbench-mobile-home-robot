"""Fail-closed policy for the controller's inbound HTTP deployment boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network

IpAddress = IPv4Address | IPv6Address
IpNetwork = IPv4Network | IPv6Network

LOCAL_TRUST_MODE = "local"
REVERSE_PROXY_TRUST_MODE = "reverse_proxy"

_ALLOWED_ADDRESS_RANGES: tuple[IpNetwork, ...] = (
    ip_network("127.0.0.0/8"),
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("::1/128"),
    ip_network("fc00::/7"),
)


class InboundHttpConfigurationError(ValueError):
    """Raised before serving when the inbound trust boundary is ambiguous."""


def _parse_address(value: str, *, setting: str) -> IpAddress:
    if not isinstance(value, str) or not value.strip():
        raise InboundHttpConfigurationError(f"{setting} must be a non-empty literal IP address")
    try:
        return ip_address(value.strip())
    except ValueError as exc:
        raise InboundHttpConfigurationError(f"{setting} must be a literal IP address") from exc


def _address_is_allowed(address: IpAddress) -> bool:
    return any(address.version == network.version and address in network for network in _ALLOWED_ADDRESS_RANGES)


def _network_is_allowed(network: IpNetwork) -> bool:
    return any(network.version == allowed.version and network.subnet_of(allowed) for allowed in _ALLOWED_ADDRESS_RANGES)


def _parse_allowlist(value: str | None) -> tuple[IpNetwork, ...]:
    if not isinstance(value, str) or not value.strip():
        raise InboundHttpConfigurationError("reverse_proxy trust mode requires a non-empty proxy allow-list")
    entries = value.split(",")
    if any(not entry.strip() for entry in entries):
        raise InboundHttpConfigurationError("proxy allow-list contains an empty entry")

    networks: list[IpNetwork] = []
    for entry in entries:
        try:
            network = ip_network(entry.strip(), strict=False)
        except ValueError as exc:
            message = "proxy allow-list entries must be literal IP addresses or CIDRs"
            raise InboundHttpConfigurationError(message) from exc
        if not _network_is_allowed(network):
            raise InboundHttpConfigurationError("proxy allow-list entries must stay within loopback or private ranges")
        if network not in networks:
            networks.append(network)
    return tuple(networks)


def validate_published_bind_address(value: str, *, setting: str) -> IpAddress:
    """Validate the host-side address a deployment publishes a service on.

    A container that listens on ``0.0.0.0`` inside its own network namespace is
    only reachable through the host publication Docker creates for it, so the
    security boundary belongs to the published address rather than to the peer
    the process sees afterwards. Compose accepts a wildcard shorthand for
    "every host interface", which is exactly the accidental-exposure default
    this function rejects: a wildcard, public, link-local, multicast or
    unresolvable value stops the service at startup instead of silently
    accepting every interface.
    """
    address = _parse_address(value, setting=setting)
    if not _address_is_allowed(address):
        raise InboundHttpConfigurationError(f"{setting} must be loopback, RFC1918 IPv4, or IPv6 unique-local")
    return address


@dataclass(frozen=True)
class InboundHttpPolicy:
    """Validated controller publication and reverse-proxy source policy."""

    published_host: str = "127.0.0.1"
    trust_mode: str = LOCAL_TRUST_MODE
    trusted_proxy_allowlist: str | None = None
    published_address: IpAddress = field(init=False)
    trusted_proxy_networks: tuple[IpNetwork, ...] = field(init=False)

    def __post_init__(self) -> None:
        address = _parse_address(self.published_host, setting="CONTROLLER_BIND_ADDRESS")
        if not _address_is_allowed(address):
            raise InboundHttpConfigurationError(
                "CONTROLLER_BIND_ADDRESS must be loopback, RFC1918 IPv4, or IPv6 unique-local"
            )
        if self.trust_mode not in {LOCAL_TRUST_MODE, REVERSE_PROXY_TRUST_MODE}:
            raise InboundHttpConfigurationError("trust mode must be local or reverse_proxy")
        if self.trust_mode == LOCAL_TRUST_MODE:
            if not address.is_loopback:
                raise InboundHttpConfigurationError("a private non-loopback bind requires reverse_proxy trust mode")
            if isinstance(self.trusted_proxy_allowlist, str) and self.trusted_proxy_allowlist.strip():
                raise InboundHttpConfigurationError("local trust mode must not supply a proxy allow-list")
            networks: tuple[IpNetwork, ...] = ()
        else:
            networks = _parse_allowlist(self.trusted_proxy_allowlist)

        object.__setattr__(self, "published_address", address)
        object.__setattr__(self, "trusted_proxy_networks", networks)

    @property
    def uses_reverse_proxy(self) -> bool:
        return self.trust_mode == REVERSE_PROXY_TRUST_MODE

    def allows_peer(self, peer_address: str) -> bool:
        if not self.uses_reverse_proxy:
            try:
                return ip_address(peer_address).is_loopback
            except ValueError:
                return False
        try:
            peer = ip_address(peer_address)
        except ValueError:
            return False
        return any(peer.version == network.version and peer in network for network in self.trusted_proxy_networks)
