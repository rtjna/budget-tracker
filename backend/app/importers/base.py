import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass
class ParsedRow:
    date: date
    description: str
    amount: Decimal  # signed: negative = money out, positive = money in


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class BankImporter(ABC):
    name: str
    provider: str
    account_kind: str
    default_account_name: str

    @abstractmethod
    def matches(self, header: list[str], sample_rows: list[list[str]]) -> bool:
        """Whether this importer recognizes the file shape."""

    @abstractmethod
    def parse(self, text: str) -> list[ParsedRow]:
        """Parse full file text into normalized rows."""
