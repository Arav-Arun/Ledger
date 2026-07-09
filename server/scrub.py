"""Deterministic PII scrub: strip card numbers, OTP, CVV, PIN before text reaches
the LLM or the store. The prompt refuses secrets too; this doesn't trust it to.
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

    # Run the regex substitutions for cards and secret keywords
    clean = CARD_RE.sub(redact_card, text)
    clean = KEYWORD_RE.sub(redact_keyword, clean)
    return clean, found
