from .amex import AmexImporter
from .barclays import BarclaysImporter
from .base import BankImporter, ParsedRow
from .revolut import RevolutImporter
from .zkb import ZkbImporter

IMPORTERS: list[BankImporter] = [
    AmexImporter(),
    BarclaysImporter(),
    RevolutImporter(),
    ZkbImporter(),
]


def detect_importer(header: list[str], sample_rows: list[list[str]]) -> BankImporter | None:
    for importer in IMPORTERS:
        if importer.matches(header, sample_rows):
            return importer
    return None
