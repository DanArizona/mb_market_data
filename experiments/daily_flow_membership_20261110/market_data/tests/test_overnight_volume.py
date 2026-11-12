import unittest

from mb_market_data.overnight_volume import (
    OvernightVolumeResult,
    derive_ov_final,
    validate_volume,
)


class TestValidateVolume(unittest.TestCase):

    def test_positive_integer(self) -> None:
        self.assertEqual(
            validate_volume(123),
            123,
        )

    def test_zero(self) -> None:
        self.assertEqual(
            validate_volume(0),
            0,
        )

    def test_negative_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_volume(-1)

    def test_float_rejected(self) -> None:
        with self.assertRaises(TypeError):
            validate_volume(123.0)

    def test_bool_rejected(self) -> None:
        with self.assertRaises(TypeError):
            validate_volume(True)


class TestDeriveOVFinal(unittest.TestCase):

    def test_spy_validation_case(self) -> None:
        self.assertEqual(
            derive_ov_final(
                ov_decision=546_802,
                candle_0925_volume=60_848,
            ),
            607_650,
        )

    def test_tsla_validation_case(self) -> None:
        self.assertEqual(
            derive_ov_final(
                ov_decision=568_675,
                candle_0925_volume=63_811,
            ),
            632_486,
        )

    def test_zero_candle_volume(self) -> None:
        self.assertEqual(
            derive_ov_final(
                ov_decision=10_000,
                candle_0925_volume=0,
            ),
            10_000,
        )

    def test_zero_ov_decision(self) -> None:
        self.assertEqual(
            derive_ov_final(
                ov_decision=0,
                candle_0925_volume=500,
            ),
            500,
        )

    def test_negative_input_rejected(self) -> None:
        with self.assertRaises(ValueError):
            derive_ov_final(
                ov_decision=-1,
                candle_0925_volume=500,
            )


class TestOvernightVolumeResult(unittest.TestCase):

    def test_derive(self) -> None:
        result = OvernightVolumeResult.derive(
            ov_decision=546_802,
            candle_0925_volume=60_848,
        )

        self.assertEqual(
            result.ov_decision,
            546_802,
        )

        self.assertEqual(
            result.candle_0925_volume,
            60_848,
        )

        self.assertEqual(
            result.ov_final,
            607_650,
        )


if __name__ == "__main__":
    unittest.main()
