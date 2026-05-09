"""Email fixtures for credit_card.email_parser tests.

Each fixture matches the dict shape returned by the ``gmail_get_email``
MCP tool (keys: ``id``, ``from``, ``subject``, ``date``, ``body``).

Provenance:
    * ``AMEX_REAL_CNP_2019`` — verbatim from a real Amex Card-Not-Present
      alert (2019), redacted: original last-5 ``61005`` preserved (it's
      a test vintage card no longer in use), merchant kept (iTunes).
      The historic plaintext omits amount, so this fixture is used only
      for the negative-case "missing amount" test.
    * All other ``*_REAL`` fixtures are absent — the user did not have
      per-transaction email alerts enabled at the time the parser
      shipped. The synthetic fixtures below were authored from each
      issuer's documented alert template (subject lines & layout that
      surface on issuer help pages and consumer reports). They should
      be replaced with redacted real samples once the user enables
      transaction alerts at each issuer (Chase Account Alerts, BofA
      Custom Alerts, FNBO Online Alerts).
"""

# --- Amex ----------------------------------------------------------------- #

AMEX_REAL_CNP_2019 = {
    "id": "16e7565696595532",
    "from": "American Express <AmericanExpress@welcome.aexp.com>",
    "to": "user@example.com",
    "subject": "Card Not Present Transaction Approved",
    "date": "Sat, 16 Nov 2019 11:06:48 -0700",
    "body": (
        "Dear GRANT KLEPZIG,\r\n\r\n\r\nYour Card was not present\r\n\r\n\r\n"
        "Hello GRANT KLEPZIG\r\n\r\nPlatinum Delta SkyMiles\r\n"
        "Ending: 61005\r\n\r\n\r\n\r\n"
        "As you requested, we're letting you know that your Card was not "
        "present at the time of this purchase.\r\n"
    ),
}

# Modern Amex large-purchase / over-threshold alert (2025+ typical layout).
AMEX_LARGE_PURCHASE = {
    "id": "amex_large_001",
    "from": "American Express <AmericanExpress@welcome.aexp.com>",
    "subject": "Large Purchase Approved",
    "date": "Mon, 05 May 2026 09:14:22 -0700",
    "body": (
        "<html><body>"
        "<table><tr><td>Hello, Grant</td></tr></table>"
        "<p>A large purchase of <strong>$245.18</strong> was approved on "
        "your Card.</p>"
        "<table>"
        "<tr><td>Amount:</td><td>$245.18</td></tr>"
        "<tr><td>Merchant:</td><td>WHOLE FOODS MARKET</td></tr>"
        "<tr><td>Date:</td><td>May 05, 2026</td></tr>"
        "<tr><td>Account Ending:</td><td>-71027</td></tr>"
        "</table>"
        "</body></html>"
    ),
}

# Inline-format Amex alert (subject and body have "$X.XX at MERCHANT" prose).
AMEX_INLINE = {
    "id": "amex_inline_001",
    "from": "AmericanExpress@welcome.aexp.com",
    "subject": "Transaction approved on your Platinum Card",
    "date": "Tue, 06 May 2026 18:32:11 -0700",
    "body": (
        "Hello, Grant\n"
        "We're letting you know about a recent charge.\n"
        "$89.42 at UBER TRIP on May 6, 2026.\n"
        "Account ending: 71027.\n"
    ),
}

AMEX_PLAN_IT_NON_TRANSACTION = {
    "id": "amex_planit_001",
    "from": "American Express <americanexpress@member.americanexpress.com>",
    "subject": "Grant, your recent transaction is eligible for Plan It®",
    "date": "Thu, 07 May 2026 17:03:19 -0600",
    "body": "Pace out your payments for your recent purchase. Account ending: 71027.",
}

AMEX_TRAVEL_INSURANCE_MARKETING = {
    "id": "amex_marketing_001",
    "from": "American Express <americanexpress@member.americanexpress.com>",
    "subject": "Grant, traveling soon? Purchase American Express® Travel Insurance",
    "date": "Wed, 08 May 2026 18:07:52 -0600",
    "body": "Explore coverage options. Account ending: 71027.",
}


# --- Chase ---------------------------------------------------------------- #

CHASE_SUBJECT_DRIVEN = {
    "id": "chase_001",
    "from": "Chase <no.reply.alerts@chase.com>",
    "subject": "Your $124.07 transaction with TRADER JOE'S #123",
    "date": "Mon, 05 May 2026 14:22:09 -0700",
    "body": (
        "<p>You made a transaction with TRADER JOE'S #123. Here are the "
        "details:</p>"
        "<table>"
        "<tr><td>Amount</td><td>$124.07</td></tr>"
        "<tr><td>Date</td><td>May 5, 2026</td></tr>"
        "<tr><td>Merchant</td><td>TRADER JOE'S #123</td></tr>"
        "<tr><td>Account ending in</td><td>3253</td></tr>"
        "</table>"
    ),
}

CHASE_BODY_FALLBACK = {
    "id": "chase_002",
    "from": "Chase <no.reply.alerts@chase.com>",
    "subject": "Transaction alert from Chase",
    "date": "Tue, 06 May 2026 09:00:00 -0700",
    "body": (
        "We are writing to let you know about a charge on your account.\n"
        "A transaction of $52.30 was made at SHELL OIL 1234567 on May 6, 2026.\n"
        "Account ending in **5502 was used.\n"
    ),
}

CHASE_DECLINED = {
    "id": "chase_decl_001",
    "from": "Chase <no.reply.alerts@chase.com>",
    "subject": "Your card was declined",
    "date": "Wed, 07 May 2026 11:00:00 -0700",
    "body": (
        "We declined a transaction on your card ending in 3253. "
        "If you recognize this attempt, please contact us."
    ),
}


# --- Bank of America (Alaska Visa) --------------------------------------- #

BOFA_PURCHASE = {
    "id": "bofa_001",
    "from": "Bank of America <onlinebanking@ealerts.bankofamerica.com>",
    "subject": "Credit card transaction exceeds alert limit",
    "date": "Wed, 07 May 2026 10:30:00 -0700",
    "body": (
        "<p>Dear Grant,</p>"
        "<p>A purchase of $312.55 was made on May 7, 2026 at "
        "ALASKA AIR 0271234567 on your account.</p>"
        "<p>Account ending in - 4365.</p>"
        "<p>If you do not recognize this transaction, please contact us.</p>"
    ),
}

BOFA_PURCHASE_PROSE = {
    "id": "bofa_002",
    "from": "Bank of America <onlinebanking@ealerts.bankofamerica.com>",
    "subject": "Account alert",
    "date": "Thu, 08 May 2026 08:15:00 -0700",
    "body": (
        "A transaction of $48.20 was made at CHEVRON 0091234 on May 8, 2026. "
        "Card ending in 4365.\n"
    ),
}

BOFA_STATEMENT_AVAILABLE = {
    "id": "bofa_stmt_001",
    "from": "Bank of America <statements@email.bankofamerica.com>",
    "subject": "Your statement is available",
    "date": "Fri, 09 May 2026 06:00:00 -0700",
    "body": (
        "Your most recent statement is available online. Account ending in 4365."
    ),
}


# --- FNBO (MGM Rewards) -------------------------------------------------- #

FNBO_PURCHASE = {
    "id": "fnbo_001",
    "from": "MGM Rewards Mastercard <alerts@card.fnbo.com>",
    "subject": "Transaction alert",
    "date": "Fri, 09 May 2026 19:45:00 -0700",
    "body": (
        "<p>Dear Cardholder,</p>"
        "<p>A transaction of $87.65 was processed on May 9, 2026 at "
        "MGM GRAND HOTEL on your card ending in 7337.</p>"
        "<p>If you did not authorize this transaction, please call us.</p>"
    ),
}

FNBO_PURCHASE_PROSE = {
    "id": "fnbo_002",
    "from": "MGM Rewards <alerts@card.fnbo.com>",
    "subject": "MGM Rewards Mastercard alert",
    "date": "Sat, 10 May 2026 12:01:00 -0700",
    "body": (
        "Hello,\n\n"
        "A purchase of $19.99 was made at NETFLIX.COM on May 10, 2026.\n"
        "Account ending 7337.\n"
    ),
}

FNBO_PAYMENT_CONFIRMATION = {
    "id": "fnbo_pay_001",
    "from": "MGM Rewards <alerts@card.fnbo.com>",
    "subject": "Payment received",
    "date": "Sun, 11 May 2026 09:00:00 -0700",
    "body": "Thank you. Your payment of $250.00 has been received. Card ending in 7337.",
}


# --- Unknown issuer ------------------------------------------------------- #

UNKNOWN_ISSUER = {
    "id": "unknown_001",
    "from": "Discover <alerts@discover.com>",
    "subject": "Transaction alert",
    "date": "Mon, 12 May 2026 10:00:00 -0700",
    "body": "A purchase of $42.00 was made at TARGET. Card ending in 9999.",
}


__all__ = [
    "AMEX_INLINE",
    "AMEX_LARGE_PURCHASE",
    "AMEX_PLAN_IT_NON_TRANSACTION",
    "AMEX_REAL_CNP_2019",
    "AMEX_TRAVEL_INSURANCE_MARKETING",
    "BOFA_PURCHASE",
    "BOFA_PURCHASE_PROSE",
    "BOFA_STATEMENT_AVAILABLE",
    "CHASE_BODY_FALLBACK",
    "CHASE_DECLINED",
    "CHASE_SUBJECT_DRIVEN",
    "FNBO_PAYMENT_CONFIRMATION",
    "FNBO_PURCHASE",
    "FNBO_PURCHASE_PROSE",
    "UNKNOWN_ISSUER",
]
