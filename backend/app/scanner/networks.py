"""Map many fiscal identities to one customer-facing retail brand."""
from __future__ import annotations

import dataclasses
import re


@dataclasses.dataclass(frozen=True)
class NetworkRef:
    name: str
    network_type: str
    store_code: str | None = None
    legal_entity_ids: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    # (VAT/tax number, legal entity ID) pairs observed on fiscal receipts.
    # A 12-digit ПН must not be truncated to obtain an 8-digit ЄДРПОУ.
    tax_identities: tuple[tuple[str, str], ...] = ()

    @property
    def edrpou(self) -> str:
        """Compatibility only: a brand does not have one unique ЄДРПОУ."""
        return self.legal_entity_ids[0] if self.legal_entity_ids else ""


NETWORKS: list[NetworkRef] = [
    # ПН 3048721… is printed on both the АТБ app receipts and the ПРРО XML.
    # It is not a source for ЄДРПОУ, so the entity half of the pair is empty.
    NetworkRef("АТБ", "supermarket", "atb", ("30487219",), ("АТБ-МАРКЕТ", "АТБ"),
               (("304872104175", ""),)),
    NetworkRef("Сільпо", "supermarket", "silpo", ("40720198",), ("СІЛЬПО", "SILPO")),
    NetworkRef("Фора", "supermarket", "fora", ("30966466",), ("ФОРА",)),
    NetworkRef("METRO", "supermarket", "metro", ("32294926", "32049199"), ("METRO",),
               (("320491926510", "32049199"),)),
    NetworkRef("Novus", "supermarket", None, ("33299901",), ("NOVUS",)),
    NetworkRef("Ашан", "supermarket", None, ("32090294",), ("АШАН", "AUCHAN")),
    NetworkRef("Епіцентр К", "home", "epicentrk", ("35590956",), ("ЕПІЦЕНТР",)),
    # ПРРО-документи ОЛДІ несуть лише ПН — ЄДРПОУ не підтверджено, тож пара
    # лишається з порожньою половиною сутності.
    NetworkRef("ОЛДИ", "home", None, (), ("ОЛДИ", "ОЛДІ", "OLDI"),
               (("247388426505", ""),)),
    NetworkRef("Нова Лінія", "home", None, ("30777091",), ("НОВА ЛІНІЯ",)),
    NetworkRef("WOG", "azs", "wog", ("38870739",), ("WOG",)),
    # Fiscal identities accumulate under the sign; a newly observed issuer must
    # not replace an older entity used by the same network.
    NetworkRef("Укрнафта", "azs", "ukrnafta", ("00135390", "00152331"),
               ("УКРНАФТА", "UKRNAFTA")),
    NetworkRef("ОККО", "azs", None, ("03158142", "3158142"), ("ОККО", "OKKO")),
    NetworkRef("БРСМ-Нафта", "azs", "brsm", ("37884671",), ("БРСМ", "BRSM")),
    NetworkRef("Фокстрот", "tech", "foxtrot", ("30012197",), ("ФОКСТРОТ",)),
    NetworkRef("Comfy", "tech", None, ("40202339",), ("COMFY",)),
    NetworkRef("Дніпро-М", "tech", "dnipro-m", ("40396815",), ("ДНІПРО-М", "DNIPRO-M")),
    NetworkRef("Аптека АНЦ", "pharmacy", None, ("41865296",), ("АПТЕКА АНЦ", "АНЦ")),
    NetworkRef("Аптека Доброго Дня", "pharmacy", None, ("38716431",),
               ("АПТЕКА ДОБРОГО ДНЯ",)),
]

_BY_ENTITY = {
    re.sub(r"\D", "", entity): network
    for network in NETWORKS
    for entity in network.legal_entity_ids
}
_BY_TAX_NUMBER = {
    re.sub(r"\D", "", tax_number): (network, entity)
    for network in NETWORKS
    for tax_number, entity in network.tax_identities
}


def identify(text: str) -> NetworkRef | None:
    """Resolve receipt text to a brand; fiscal ID first, sign alias second."""
    raw = text or ""
    digits = re.sub(r"\D", "", raw)
    for entity, network in _BY_ENTITY.items():
        if entity and entity in digits:
            return network
    folded = raw.upper().replace("’", "'")
    for network in NETWORKS:
        for alias in network.aliases:
            if re.search(rf"(?<!\w){re.escape(alias.upper())}(?!\w)", folded):
                return network
    return None


def by_edrpou(edrpou: str) -> NetworkRef | None:
    """Resolve any known legal entity/issuer to its visible brand."""
    return _BY_ENTITY.get(re.sub(r"\D", "", edrpou or ""))


def by_tax_number(tax_number: str) -> tuple[NetworkRef, str] | None:
    """Resolve a VAT/tax number to its brand and confirmed legal entity ID.

    The entity half is empty when the ПН identifies the brand but no ЄДРПОУ has
    been confirmed for it — never derive one from the ПН digits."""
    return _BY_TAX_NUMBER.get(re.sub(r"\D", "", tax_number or ""))
