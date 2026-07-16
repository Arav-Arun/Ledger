"""Deterministic PII scrub: strip card numbers, OTP, CVV, PIN, account numbers and
SSNs before text reaches the LLM or the store. The prompt refuses secrets too; this
doesn't trust it to.
"""

import re

# Regular expression to match common credit/debit card numbers (13 to 19 digits)
# Allowing optional spaces or dashes between blocks of numbers.
CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

# Regular expression to match authentication secrets (like OTP, PIN, password)
# followed by typical separators (is, was, :, =, etc.) and the actual value.
KEYWORD_RE = re.compile(
    r"\b(otp|cvv|cvc|pin|password|passcode)\b(?:\s*(?:is|was|:|-|=))?\s*\S+",
    re.IGNORECASE,
)

# Account numbers are structureless digits (indistinguishable from an order id), so -
# like OTP/PIN - we only redact them when introduced by an explicit account keyword
# followed by a run of >=7 digits. This avoids nuking phrases like "my account is locked".
ACCOUNT_RE = re.compile(
    r"\b(?:account|acct|a/c)\b(?:\s*(?:number|no\.?|#|is|was|:|-|=))*\s*(\d[\d -]{5,}\d)",
    re.IGNORECASE,
)

# US Social Security numbers have a fixed, self-identifying shape, so a bare pattern
# match is safe enough to redact without a leading keyword.
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def luhn_valid(digits: str) -> bool:
    """Performs the Luhn Algorithm (mod 10 check) to verify if a sequence of
    digits represents a structurally valid credit card number.
    
    This avoids false positive redactions on random strings of numbers.
    """
    total, alt = 0, False
    # Process digits in reverse order
    for d in reversed(digits):
        n = int(d)
        if alt:
            # Double every second digit
            n *= 2
            # If doubling results in a number greater than 9, subtract 9
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    # If the total sum is divisible by 10, the card number is structurally valid
    return total % 10 == 0


def scrub(text: str) -> tuple[str, list[str]]:
    """Scrubs sensitive information (PII) from user input before sending to the LLM or DB.
    
    Returns:
        tuple[str, list[str]]: (cleaned_text, list of redacted label strings)
    """
    found: list[str] = []

    def redact_card(m: re.Match) -> str:
        """Regex replacement callback for credit card matches.
        
        Extracts pure digits, verifies Luhn validity, and replaces with placeholder if valid.
        """
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and luhn_valid(digits):
            found.append("card number")
            return "[card number removed]"
        return m.group()

    def redact_keyword(m: re.Match) -> str:
        """Regex replacement callback for authentication keywords (OTP, passwords).

        Replaces the matched secret with a secure placeholder string.
        """
        found.append(m.group(1).lower())
        return f"[{m.group(1).lower()} removed]"

    def redact_account(m: re.Match) -> str:
        """Redacts the digit run of an account number, keeping the surrounding keyword."""
        found.append("account number")
        return m.group().replace(m.group(1), "[account number removed]")

    def redact_ssn(m: re.Match) -> str:
        """Regex replacement callback for US Social Security numbers."""
        found.append("ssn")
        return "[ssn removed]"

    # Run the regex substitutions. Cards first so a Luhn-valid card that also follows an
    # "account" keyword is caught as a card; then keyword secrets, account numbers, SSNs.
    clean = CARD_RE.sub(redact_card, text)
    clean = KEYWORD_RE.sub(redact_keyword, clean)
    clean = ACCOUNT_RE.sub(redact_account, clean)
    clean = SSN_RE.sub(redact_ssn, clean)
    return clean, found
