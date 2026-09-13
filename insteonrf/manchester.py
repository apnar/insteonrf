"""Manchester (bi-phase level) coding as used by the Insteon RF framing.

Bit strings are ASCII: one ``'0'`` / ``'1'`` character per bit. A logical
``0`` is sent as the pair ``10`` and a logical ``1`` as ``01``.
"""

from __future__ import annotations


class ManchesterError(ValueError):
    """Raised when a bit pair is not a valid Manchester symbol (``00`` or ``11``)."""


_ENCODE = {"0": "10", "1": "01"}
_DECODE = {"10": "0", "01": "1"}


def manchester_encode(bits: str) -> str:
    """Encode a string of ``'0'``/``'1'`` characters, doubling its length."""
    try:
        return "".join(_ENCODE[b] for b in bits)
    except KeyError as err:
        raise ValueError(f"invalid bit {err.args[0]!r}") from None


def manchester_decode(bits: str) -> str:
    """Decode Manchester pairs back to a bit string.

    Raises :class:`ManchesterError` on an invalid pair. A trailing odd bit is
    ignored.
    """
    out = []
    for i in range(0, len(bits) - 1, 2):
        pair = bits[i : i + 2]
        try:
            out.append(_DECODE[pair])
        except KeyError:
            raise ManchesterError(f"invalid Manchester pair {pair!r} at bit {i}") from None
    return "".join(out)


def invert_bits(bits: str) -> str:
    """Flip every bit of an ASCII bit string."""
    return bits.translate(_INVERT_TABLE)


_INVERT_TABLE = str.maketrans("01", "10")
