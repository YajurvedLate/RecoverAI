import unittest

from app.models import Payment
from app.risk_engine import (
    PaymentNotFailedError,
    UnsupportedFailureReasonError,
    score_payment,
)

PAISE_PER_RUPEE = 100


def _failed_payment(failure_reason: str, amount_rupees: int) -> Payment:
    return Payment(
        merchant_id=1,
        customer_id="CUST-0001",
        amount=amount_rupees * PAISE_PER_RUPEE,
        currency="INR",
        status="FAILED",
        failure_reason=failure_reason,
    )


class ScorePaymentTests(unittest.TestCase):
    def test_bank_timeout_below_1000_is_medium_60(self) -> None:
        result = score_payment(_failed_payment("BANK_TIMEOUT", 999))
        self.assertEqual(result.score, 60)
        self.assertEqual(result.priority, "MEDIUM")

    def test_bank_server_down_20000_is_high_85(self) -> None:
        result = score_payment(_failed_payment("BANK_SERVER_DOWN", 20_000))
        self.assertEqual(result.score, 85)
        self.assertEqual(result.priority, "HIGH")

    def test_network_error_between_1000_and_4999_is_medium_60(self) -> None:
        result = score_payment(_failed_payment("NETWORK_ERROR", 2_500))
        self.assertEqual(result.score, 60)
        self.assertEqual(result.priority, "MEDIUM")

    def test_card_expired_below_1000_is_low_20(self) -> None:
        result = score_payment(_failed_payment("CARD_EXPIRED", 500))
        self.assertEqual(result.score, 20)
        self.assertEqual(result.priority, "LOW")

    def test_insufficient_funds_at_least_10000_is_medium_55(self) -> None:
        result = score_payment(_failed_payment("INSUFFICIENT_FUNDS", 10_000))
        self.assertEqual(result.score, 55)
        self.assertEqual(result.priority, "MEDIUM")


class InvalidPaymentTests(unittest.TestCase):
    def test_success_payment_raises_payment_not_failed(self) -> None:
        payment = Payment(
            merchant_id=1,
            customer_id="CUST-0001",
            amount=1_000 * PAISE_PER_RUPEE,
            currency="INR",
            status="SUCCESS",
            failure_reason=None,
        )
        with self.assertRaises(PaymentNotFailedError):
            score_payment(payment)

    def test_unsupported_failure_reason_raises(self) -> None:
        with self.assertRaises(UnsupportedFailureReasonError):
            score_payment(_failed_payment("NOT_A_REAL_REASON", 1_000))

    def test_insufficient_balance_is_unsupported(self) -> None:
        with self.assertRaises(UnsupportedFailureReasonError):
            score_payment(_failed_payment("INSUFFICIENT_BALANCE", 1_000))


class AmountBoundaryTests(unittest.TestCase):
    def test_amount_boundaries_use_expected_bonuses(self) -> None:
        # BANK_TIMEOUT base score is 55; bonus is inferred from the final score.
        cases = (
            (999, 60),
            (1_000, 65),
            (4_999, 65),
            (5_000, 75),
            (9_999, 75),
            (10_000, 85),
        )
        for amount_rupees, expected_score in cases:
            with self.subTest(amount_rupees=amount_rupees):
                result = score_payment(_failed_payment("BANK_TIMEOUT", amount_rupees))
                self.assertEqual(result.score, expected_score)


class ReasonTextTests(unittest.TestCase):
    def test_reason_is_deterministic_and_includes_main_factors(self) -> None:
        payment = _failed_payment("BANK_TIMEOUT", 999)
        first = score_payment(payment)
        second = score_payment(payment)
        self.assertEqual(first.reason, second.reason)
        self.assertIn("BANK_TIMEOUT", first.reason)
        self.assertIn("below ₹1,000", first.reason)
        self.assertIn("60", first.reason)
        self.assertIn("MEDIUM", first.reason)


if __name__ == "__main__":
    unittest.main()
