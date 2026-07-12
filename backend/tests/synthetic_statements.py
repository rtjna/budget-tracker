"""Builders for synthetic scrambled statement page-streams.

These construct the post-extraction word streams the parsers consume, by laying
out known plaintext at positions and applying a random per-font substitution
cipher (including the capital-B -> newline sentinel and Tahoma's zero-width 'y').
They let the full derivation + layout + reconciliation pipeline be exercised
without a real PDF. Not a test module itself (no ``test_`` prefix).
"""

import random

from app.importers.pdf_cipher import NEWLINE_SENTINEL

_GLYPHS = [chr(c) for c in range(0x2100, 0x2600)]


def make_cipher(chars, seed, *, newline_char=None):
    """Random 1:1 plaintext-char -> glyph map. ``newline_char`` (e.g. 'B') is
    forced onto the newline sentinel to mimic the glyph that extracts as '\\n'."""
    rnd = random.Random(seed)
    pool = _GLYPHS[:]
    rnd.shuffle(pool)
    cipher = {}
    i = 0
    for ch in sorted(set(chars)):
        if ch == " ":
            continue
        if ch == newline_char:
            cipher[ch] = NEWLINE_SENTINEL
        else:
            cipher[ch] = pool[i]
            i += 1
    return cipher


def scramble(text, cipher, *, drop=None):
    out = []
    for ch in text:
        if ch == " ":
            out.append(" ")
        elif drop is not None and ch == drop:
            continue  # zero-width glyph
        else:
            out.append(cipher.get(ch, ch))
    return "".join(out)


def word(font, text, x0, top):
    return (font, text, x0, top)


# ---------------------------------------------------------------------------
# Barclays debit statement
# ---------------------------------------------------------------------------

BODY = "Expert-Sans-Regular"
BOLD = "Expert-Sans-RegularBold"


def barclays_words():
    """A one-page Barclays statement: start balance 1,000.00, one money-in and
    two money-out rows, reconciling glance totals and running balances."""
    w = word
    return [
        # bold header row
        w(BOLD, "Date", 50, 100), w(BOLD, "Description", 90, 100),
        w(BOLD, "Money", 300, 100), w(BOLD, "out", 345, 100),
        w(BOLD, "Money", 400, 100), w(BOLD, "in", 445, 100),
        w(BOLD, "Balance", 500, 100),
        # constant body text (anchors) + glance totals
        w(BODY, "Your", 50, 40), w(BODY, "Barclays", 75, 40), w(BODY, "Bank", 120, 40),
        w(BODY, "Account", 150, 40), w(BODY, "statement", 190, 40),
        w(BODY, "Statement", 50, 55), w(BODY, "date", 110, 55),
        w(BODY, "Last", 50, 62), w(BODY, "statement", 80, 62),
        w(BODY, "Money", 50, 70), w(BODY, "in", 90, 70), w(BODY, "£500", 110, 70), w(BODY, "00", 140, 70),
        w(BODY, "Money", 50, 78), w(BODY, "out", 90, 78), w(BODY, "£240", 110, 78), w(BODY, "00", 140, 78),
        w(BODY, "02", 50, 90), w(BODY, "Jun", 70, 90), w(BODY, "-", 95, 90),
        w(BODY, "01", 110, 90), w(BODY, "Jul", 130, 90), w(BODY, "2026", 155, 90),
        # table
        w(BODY, "Start", 50, 120), w(BODY, "balance", 80, 120),
        w(BODY, "1,000", 500, 120), w(BODY, "00", 530, 120),
        w(BODY, "02", 50, 140), w(BODY, "Jun", 70, 140), w(BODY, "Payment", 100, 140), w(BODY, "Received", 150, 140),
        w(BODY, "500", 400, 140), w(BODY, "00", 430, 140), w(BODY, "1,500", 500, 140), w(BODY, "00", 530, 140),
        w(BODY, "05", 50, 160), w(BODY, "Jun", 70, 160), w(BODY, "Coffee", 100, 160), w(BODY, "Shop", 150, 160),
        w(BODY, "40", 300, 160), w(BODY, "00", 330, 160), w(BODY, "1,460", 500, 160), w(BODY, "00", 530, 160),
        w(BODY, "10", 50, 180), w(BODY, "Jul", 70, 180), w(BODY, "Rent", 100, 180), w(BODY, "Bakshi", 150, 180),
        w(BODY, "200", 300, 180), w(BODY, "00", 330, 180), w(BODY, "1,260", 500, 180), w(BODY, "00", 530, 180),
        # break note + footer (source of the digit + capital anchors)
        w(BODY, "Anything", 50, 250), w(BODY, "Wrong", 95, 250),
        w(BODY, "Authorised", 50, 300), w(BODY, "by", 95, 300), w(BODY, "the", 110, 300),
        w(BODY, "Prudential", 130, 300), w(BODY, "Regulation", 175, 300), w(BODY, "Authority", 220, 300),
        w(BODY, "Financial", 50, 310), w(BODY, "Services", 95, 310), w(BODY, "Register", 140, 310),
        w(BODY, "number", 185, 310), w(BODY, "759676", 220, 310),
    ]


def barclays_pages(seed):
    words = barclays_words()
    chars = "".join(t for _, t, _, _ in words)
    cipher = make_cipher(chars, seed, newline_char="B")
    body, bold = [], []
    for font, text, x0, top in words:
        rec = {
            "text": scramble(text, cipher),
            "x0": x0, "x1": x0 + 6 * len(text), "top": top,
            "fontname": "ABCDEF+" + font,
        }
        (body if font == BODY else bold).append(rec)
    return [{"body": body, "bold": bold}]


# ---------------------------------------------------------------------------
# Barclaycard statement
# ---------------------------------------------------------------------------

CO = "Barclaycard-Co"
ROW = "Tahoma"


def barclaycard_words():
    """A one-page Barclaycard statement: previous 600, payments 2,000 (one row),
    card use 1,760 (two rows), new 360; identity 600 - 2000 + 1760 = 360."""
    w = word
    return [
        # Co font
        w(CO, "Your", 50, 20), w(CO, "transactions", 80, 20),
        w(CO, "Please", 50, 30), w(CO, "pay", 90, 30), w(CO, "by", 110, 30),
        w(CO, "27", 130, 30), w(CO, "July", 150, 30), w(CO, "2026", 180, 30),
        w(CO, "0800", 50, 40), w(CO, "151", 80, 40), w(CO, "0900", 110, 40),
        w(CO, "0333", 50, 45), w(CO, "200", 80, 45), w(CO, "9090", 110, 45),
        w(CO, "Your", 50, 100), w(CO, "previous", 80, 100), w(CO, "balance", 130, 100), w(CO, "£600.00", 250, 100),
        w(CO, "Payments", 50, 150), w(CO, "towards", 95, 150), w(CO, "your", 140, 150), w(CO, "account", 170, 150), w(CO, "£2,000.00", 250, 150),
        w(CO, "How", 50, 250), w(CO, "you've", 75, 250), w(CO, "used", 110, 250), w(CO, "your", 140, 250), w(CO, "card", 170, 250), w(CO, "£1,760.00", 250, 250),
        w(CO, "Your", 50, 400), w(CO, "new", 80, 400), w(CO, "balance", 110, 400), w(CO, "£360.00", 250, 400),
        w(CO, "Minimum", 50, 420), w(CO, "payment", 95, 420), w(CO, "£25.00", 250, 420),
        # Tahoma rows
        w(ROW, "15", 40, 200), w(ROW, "Jun", 65, 200), w(ROW, "Payment", 95, 200), w(ROW, "thank", 140, 200), w(ROW, "you", 175, 200), w(ROW, "£2,000.00", 250, 200),
        w(ROW, "10", 40, 300), w(ROW, "Jun", 65, 300), w(ROW, "Coffee", 95, 300), w(ROW, "£10.00", 250, 300),
        w(ROW, "12", 40, 320), w(ROW, "Jun", 65, 320), w(ROW, "Groceries", 95, 320), w(ROW, "£1,750.00", 250, 320),
        # Tahoma informational anchor rows (below new balance; carry no date)
        w(ROW, "You", 40, 500), w(ROW, "had", 65, 500), w(ROW, "no", 90, 500), w(ROW, "promotional", 110, 500), w(ROW, "transactions", 170, 500),
        w(ROW, "You", 40, 520), w(ROW, "had", 65, 520), w(ROW, "no", 90, 520), w(ROW, "charges", 110, 520), w(ROW, "or", 150, 520), w(ROW, "interest", 170, 520), w(ROW, "transactions", 220, 520),
    ]


def barclaycard_pages(seed):
    words = barclaycard_words()
    co_chars = "".join(t for f, t, _, _ in words if f == CO)
    row_chars = "".join(t for f, t, _, _ in words if f == ROW)
    co_cipher = make_cipher(co_chars, seed)
    row_cipher = make_cipher(row_chars, seed + 7919)
    co, row = [], []
    for font, text, x0, top in words:
        if font == CO:
            rec_text = scramble(text, co_cipher)
            target = co
        else:
            rec_text = scramble(text, row_cipher, drop="y")  # zero-width 'y'
            target = row
        target.append({
            "text": rec_text,
            "x0": x0, "x1": x0 + 6 * len(text), "top": top,
            "fontname": "ABCDEF+" + font,
        })
    return [{"co": co, "row": row}]
