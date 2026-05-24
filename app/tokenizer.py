import tiktoken

_encoder = None

def get_token_count(text: str) -> int:
    global _encoder
    if _encoder is None:
        _encoder = tiktoken.get_encoding("cl100k_base")
    if not text:
        return 0
    return len(_encoder.encode(text))

def count_words(text: str) -> int:
    """Count words by splitting on whitespace."""
    if not text:
        return 0
    return len(text.split())
