"""Registers of a shop ("caixas") as the cloud sees them.

The shop's server computer is the only Terminal; the registers linked to it
("estações") are a local concept. The cloud only limits how many may exist
(licence claim of the shop's hub terminal) and labels cash sessions and sales
with the register that made them.
"""
import re

from .models import Terminal

STATION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
STATION_NAME_MAX = 80
# Label the terminal gives to the shop's server computer ("Caixa principal").
MAIN_STATION_ID = "server"
STATIONS_MAX = 64


def effective_hub_id(shop):
    """Terminal that is the shop's main register: the one chosen, while it is
    paired and not revoked, else the oldest paired terminal of the shop that is not revoked."""
    if shop.hub_terminal_id:
        # A terminal with no computer cannot be the main register: choosing it would take the
        # additional registers away from the one in use while nobody answers as the new one.
        if Terminal.objects.filter(pk=shop.hub_terminal_id, shop=shop, revoked=False,
                                   token_hash__isnull=False).exists():
            return shop.hub_terminal_id
    return (Terminal.objects.filter(shop=shop, revoked=False, token_hash__isnull=False)
            .order_by("created_at", "id").values_list("id", flat=True).first())


def is_hub(terminal):
    return not terminal.revoked and effective_hub_id(terminal.shop) == terminal.id


def stations_allowed(terminal, plan):
    """Additional registers this terminal may link: the plan's amount for the
    shop's hub, zero for every other terminal of the shop."""
    return plan.max_stations if is_hub(terminal) else 0


def station_of(event):
    """(id, name) of the register that produced an event. A missing or malformed
    label is ("", ""): the label never refuses what happened at the counter."""
    station = event.get("station") if isinstance(event, dict) else None
    if not isinstance(station, dict) or set(station) != {"id", "name"}:
        return "", ""
    station_id, name = station["id"], station["name"]
    if (not isinstance(station_id, str) or not STATION_ID.fullmatch(station_id)
            or not isinstance(name, str) or len(name) > STATION_NAME_MAX):
        return "", ""
    return station_id, name


def valid_stations(value):
    """Shape of terminal.config "stations": up to 64 of exactly {"id","name","revoked"}."""
    if not isinstance(value, list) or len(value) > STATIONS_MAX:
        return False
    for row in value:
        if (not isinstance(row, dict) or set(row) != {"id", "name", "revoked"}
                or not isinstance(row["id"], str) or not STATION_ID.fullmatch(row["id"])
                or not isinstance(row["name"], str) or len(row["name"]) > STATION_NAME_MAX
                or type(row["revoked"]) is not bool):
            return False
    return True


def config_stations(terminal):
    """Registers the terminal last reported, always well formed."""
    config = terminal.config if isinstance(terminal.config, dict) else {}
    value = config.get("stations")
    if not value or not valid_stations(value):
        return []
    return [{"id": row["id"], "name": row["name"], "revoked": row["revoked"]} for row in value]
