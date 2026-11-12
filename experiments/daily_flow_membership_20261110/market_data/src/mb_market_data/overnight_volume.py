"""
Overnight-volume calculations.

Definitions
-----------
OV_DECISION
    Volume for 01:00 <= ET < 09:25.

OV_FINAL
    Volume for 01:00 <= ET < 09:30.

For 5-minute Schwab candles:

    OV_FINAL = OV_DECISION + volume of the 09:25 ET candle

The 09:25 candle covers the interval:

    09:25 <= ET < 09:30
"""

from __future__ import annotations

from dataclasses import dataclass


def validate_volume(
    value: int,
    *,
    name: str = "volume",
) -> int:
    """
    Validate a volume value.

    Volume must be a nonnegative Python integer.

    bool is rejected explicitly even though bool is a subclass of int.
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{name} must be an integer, "
            f"got {type(value).__name__}"
        )

    if value < 0:
        raise ValueError(
            f"{name} must be nonnegative, got {value}"
        )

    return value


def derive_ov_final(
    ov_decision: int,
    candle_0925_volume: int,
) -> int:
    """
    Derive OV_FINAL from OV_DECISION and the 09:25 ET candle.

    Parameters
    ----------
    ov_decision
        Volume accumulated during 01:00 <= ET < 09:25.

    candle_0925_volume
        Volume of the Schwab five-minute candle beginning at
        09:25 ET and ending immediately before 09:30 ET.

    Returns
    -------
    int
        Volume during 01:00 <= ET < 09:30.
    """

    ov_decision = validate_volume(
        ov_decision,
        name="ov_decision",
    )

    candle_0925_volume = validate_volume(
        candle_0925_volume,
        name="candle_0925_volume",
    )

    return ov_decision + candle_0925_volume


@dataclass(frozen=True)
class OvernightVolumeResult:
    """
    Components of a derived OV_FINAL value.
    """

    ov_decision: int
    candle_0925_volume: int
    ov_final: int

    @classmethod
    def derive(
        cls,
        ov_decision: int,
        candle_0925_volume: int,
    ) -> "OvernightVolumeResult":
        """
        Construct a validated overnight-volume result.
        """

        validated_ov_decision = validate_volume(
            ov_decision,
            name="ov_decision",
        )

        validated_candle_volume = validate_volume(
            candle_0925_volume,
            name="candle_0925_volume",
        )

        ov_final = derive_ov_final(
            validated_ov_decision,
            validated_candle_volume,
        )

        return cls(
            ov_decision=validated_ov_decision,
            candle_0925_volume=validated_candle_volume,
            ov_final=ov_final,
        )
