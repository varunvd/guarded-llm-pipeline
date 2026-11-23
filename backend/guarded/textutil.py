"""Small text helpers shared by validation, risk rules and the simulator."""

from __future__ import annotations

import re

_STOPWORDS = {
    "en": "the and you your for with that this have not are was but will can our has from they "
    "would there been what when which about please thanks thank".split(),
    "es": "el la los las de que y en un una por para con no su se lo le mi es pero como más "
    "gracias hola pedido quiero tengo fue está muy".split(),
    "fr": "le la les des que et en un une pour avec pas vous je est mais comme plus merci "
    "bonjour commande veux suis mon".split(),
    "de": "der die das und ich nicht sie ist mit für ein eine auf dass aber wie mehr danke "
    "hallo bestellung möchte habe mein".split(),
}
_STOPSETS = {lang: frozenset(words) for lang, words in _STOPWORDS.items()}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def detect_language(text: str) -> str | None:
    """Best guess among en/es/fr/de, or None when the text gives too little signal."""
    words = _WORD.findall(text.lower())
    scores = {lang: sum(w in stop for w in words) for lang, stop in _STOPSETS.items()}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (best, best_score), (_, second_score) = ranked[0], ranked[1]
    if best_score < 3 or best_score < second_score * 1.5:
        return None
    return best


def first_name(full_name: str) -> str:
    return full_name.split()[0] if full_name.strip() else "there"


def negated(text: str, start: int, window: int = 28) -> bool:
    """True when the words just before `start` negate what follows."""
    before = text[max(0, start - window) : start].lower()
    return bool(
        re.search(
            r"\b(not|no|never|cannot|can't|cant|unable|don't|doesn't|won't|isn't|aren't)\b"
            r"|n't\b|\bno\s+(podemos|ofrecemos|garantizamos)\b",
            before,
        )
    )
