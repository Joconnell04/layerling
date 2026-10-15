"""Map SAM 3 prompts to PART_VOCAB parts, and rank parts by how specific they are.

The ranking settles conflicts in back-projection: when two labels both claim a face with enough
votes, the more specific part (eye inside head, nose inside muzzle) wins if it is confident.
"""

from __future__ import annotations

import re

from p2p_contracts.payloads import PART_VOCAB

# Lower rank = smaller, more specific part.
SPECIFICITY: dict[str, int] = {
    "pupil": 0,
    "iris": 0,
    "eye_highlight": 0,
    "eye": 1,
    "nose": 1,
    "mouth": 1,
    "tongue": 1,
    "teeth": 1,
    "paw_pad": 1,
    "inner_ear": 1,
    "eyebrow": 1,
    "tag": 1,
    "text": 1,
    "ear": 2,
    "muzzle": 2,
    "marking": 2,
    "collar": 2,
    "paw": 2,
    "keyring": 2,
    "face": 3,
    "tail": 3,
    "accessory": 3,
    "other": 3,
    "head": 4,
    "body": 5,
    "base": 5,
}
PAIRED_PARTS = frozenset({"eye", "ear", "inner_ear", "eyebrow", "pupil", "iris", "paw", "paw_pad"})

_PHRASES: dict[str, str] = {
    "inner ear": "inner_ear",
    "ear inside": "inner_ear",
    "paw pad": "paw_pad",
    "toe bean": "paw_pad",
    "toe beans": "paw_pad",
    "eye highlight": "eye_highlight",
    "eye shine": "eye_highlight",
    "catch light": "eye_highlight",
    "key ring": "keyring",
    "name tag": "tag",
}
_WORDS: dict[str, str] = {
    "eyes": "eye",
    "eyeball": "eye",
    "eyeballs": "eye",
    "pupils": "pupil",
    "irises": "iris",
    "nostril": "nose",
    "nostrils": "nose",
    "snout": "muzzle",
    "ears": "ear",
    "eyebrows": "eyebrow",
    "brow": "eyebrow",
    "brows": "eyebrow",
    "lips": "mouth",
    "lip": "mouth",
    "tooth": "teeth",
    "fang": "teeth",
    "fangs": "teeth",
    "paws": "paw",
    "foot": "paw",
    "feet": "paw",
    "pads": "paw_pad",
    "tails": "tail",
    "spot": "marking",
    "spots": "marking",
    "patch": "marking",
    "patches": "marking",
    "stripe": "marking",
    "stripes": "marking",
    "markings": "marking",
    "blaze": "marking",
    "socks": "marking",
    "torso": "body",
    "fur": "body",
    "pedestal": "base",
    "stand": "base",
    "plinth": "base",
    "keychain": "keyring",
    "loop": "keyring",
    "letters": "text",
    "lettering": "text",
    "hat": "accessory",
    "bow": "accessory",
    "bandana": "accessory",
    "scarf": "accessory",
    "harness": "collar",
}
_PREPOSITIONS = re.compile(r"\b(on|of|with|in|near|around|at|under|over|above|below|behind)\b")


def part_for_prompt(prompt: str) -> str:
    """'left cat eyes' -> 'eye'; 'black patch on the back' -> 'marking'; unknown -> 'other'."""
    text = " ".join(re.findall(r"[a-z_]+", prompt.lower()))
    text = _PREPOSITIONS.split(text)[0].strip() or text
    for phrase, part in _PHRASES.items():
        if re.search(rf"\b{phrase}\b", text):
            return part
    found = "other"
    for word in text.split():
        if word in PART_VOCAB:
            found = word
        elif word in _WORDS:
            found = _WORDS[word]
    return found


def specificity(part: str) -> int:
    return SPECIFICITY.get(part, SPECIFICITY["other"])
