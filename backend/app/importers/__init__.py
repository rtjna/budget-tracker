from .amex import AmexImporter
from .base import BankImporter, ParsedRow

IMPORTERS: list[BankImporter] = [AmexImporter()]


def detect_importer(header: list[str], sample_rows: list[list[str]]) -> BankImporter | None:
    for importer in IMPORTERS:
        if importer.matches(header, sample_rows):
            return importer
    return None
