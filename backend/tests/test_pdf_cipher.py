"""Unit tests for the PDF-free cipher derivation layer.

These build scrambled word streams by applying random substitution ciphers to
known plaintext, then assert the derivation recovers the map -- no real PDF is
needed.
"""

import random

import pytest

from app.importers.pdf_cipher import (
    StatementDecodeError,
    apply_map,
    derive_substitution,
    isomorph,
    iter_numeric_maps,
    resolve_separators,
    solve_numeric,
)

GLYPHS = [chr(c) for c in range(0x2100, 0x2600)]


def random_cipher(chars, seed):
    """A random 1:1 plaintext-char -> glyph map."""
    rnd = random.Random(seed)
    pool = GLYPHS[:]
    rnd.shuffle(pool)
    return {ch: pool[i] for i, ch in enumerate(sorted(set(chars)))}


def scramble(text, cipher, drop=""):
    return "".join("" if ch == drop else cipher.get(ch, ch) for ch in text)


def test_isomorph_matches_under_substitution():
    assert isomorph("balance") == (0, 1, 2, 1, 3, 4, 5)
    # A substitution preserves the isomorph ("balance" <-> "qxyxadf").
    assert isomorph("balance") == isomorph("qxyxadf")
    assert isomorph("Money") == isomorph("XyZab")
    assert isomorph("aa") != isomorph("ab")


def test_derive_recovers_letters_from_anchors():
    plaintext = [
        "Your Barclays Bank Account statement",
        "Start balance",
        "Money in",
        "Money out",
        "Anything Wrong",
    ]
    anchors = list(plaintext)
    for seed in range(20):
        cipher = random_cipher("".join(plaintext), seed)
        stream = []
        for line in plaintext:
            stream += [scramble(w, cipher) for w in line.split()]
        glyph_map = derive_substitution(stream, anchors)
        # Every letter that appears in the anchors is recovered, correctly.
        for ch in set("".join(plaintext)) - {" "}:
            assert cipher[ch] in glyph_map
            assert glyph_map[cipher[ch]] == ch


def test_derive_is_one_to_one_and_ignores_absent_anchors():
    plaintext = "Money in Money out"
    cipher = random_cipher(plaintext, 1)
    stream = [scramble(w, cipher) for w in plaintext.split()]
    glyph_map = derive_substitution(stream, ["Money in", "Money out", "Never Appears Here"])
    # 1:1 -- no glyph maps to two chars and no char from two glyphs.
    assert len(set(glyph_map.values())) == len(glyph_map)
    assert apply_map(stream[0], glyph_map) == "Money"


def test_resolve_separators_finds_currency_comma_decimal():
    cipher = random_cipher("£0123456789,.", 5)
    tokens = [scramble(t, cipher) for t in ("£1,234.56", "£70.00", "£2,000.00")]
    glyph_map = {}  # digits unknown, as during real derivation
    resolve_separators(tokens, glyph_map)
    assert glyph_map[cipher["."]] == "."
    assert glyph_map[cipher[","]] == ","
    assert glyph_map[cipher["£"]] == "£"
    # A 3-digit amount's leading currency glyph is not mistaken for a comma.
    assert cipher["£"] != cipher[","]


def test_solve_numeric_completes_digits_via_oracle():
    # Two digit glyphs unknown; the oracle accepts only the true assignment.
    base = {"A": "1", "B": "0"}  # 1 and 0 already known
    truth = {"X": "5", "Y": "9"}
    full = dict(base, **truth)

    def oracle(candidate):
        return candidate.get("X") == "5" and candidate.get("Y") == "9"

    solved = solve_numeric(base, ["X", "Y"], oracle)
    assert solved == full


def test_solve_numeric_refuses_when_search_space_too_large():
    # Nothing anchored -> 10 unknown glyphs against 10 free digits is refused.
    with pytest.raises(StatementDecodeError, match="too few numeric anchors"):
        solve_numeric({}, list("abcdefghij"), lambda m: True, max_perms=1000)


def test_iter_numeric_maps_yields_all_accepted():
    # X in {0,3} both accepted -> both yielded, letting a caller disambiguate.
    seen = [m["X"] for m in iter_numeric_maps({}, ["X"], lambda m: m["X"] in {"0", "3"})]
    assert sorted(seen) == ["0", "3"]
