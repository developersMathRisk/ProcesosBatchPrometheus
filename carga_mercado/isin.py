"""Validación del dígito verificador de un ISIN (ISO 6166)."""
import re


def isin_valido(isin: str) -> bool:
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin or ""):
        return False
    digitos = "".join(str(int(c, 36)) for c in isin)  # letras -> 10..35
    total = 0
    for i, ch in enumerate(reversed(digitos)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0
