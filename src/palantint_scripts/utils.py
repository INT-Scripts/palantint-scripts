import re
import unidecode

def normalize_name(name_str: str) -> str:
    """
    Standardizes names for matching (lowercase, alphanumeric only).
    Removes accents and special characters.
    """
    if not name_str: return ""
    # Remove accents
    n = unidecode.unidecode(name_str)
    # Alphanumeric only, lowercase
    n = re.sub(r'[^a-zA-Z0-9]', '', n).lower()
    return n
