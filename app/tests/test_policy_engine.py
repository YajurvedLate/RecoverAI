import unittest

from app.models import Payment, RecoveryCase
from app.policy_engine import PolicyDecision, validate_action


PAISE_PER_RUPEE = 100


def _recovery_case(
    status: str = "OPEN",
    payment_id: int = 1,
    risk_score: int = 60,
    priority: str = "MEDIUM",
) -> RecoveryCase:
    """Create a test recovery case without database."""
    case = RecoveryCase(
        id=1,
        payment_id=payment_id,
        status=status,
        risk_score=risk_score,
        priority=priority,
    )
    return case


def _payment(
    status: str = "FAILED",
    failure_reason: str = "BANK_TIMEOUT",
    amount_rupees: int = 1000,
) -> Payment:
    """Create a test payment without database."""
    payment = Payment(
        id=1,
        merchant_id=1,
        customer_id="CUST-0001",
        amount=amount_rupees * PAISE_PER_RUPEE,
        currency="INR",
        status=status,
        failure_reason=failure_reason,
    )
    return payment


class TemporaryFailureRetryTests(unittest.TestCase):
    """Test that RETRY_PAYMENT is allowed for temporary failures on OPEN cases."""

    def test_bank_timeout_retry_is_allowed(self) -> None:
        """OPEN case + FAILED payment + BANK_TIMEOUT + RETRY_PAYMENT -> allowed."""
        case = _recovery_case(status="OPEN")
        payment = _payment(failure_reason="BANK_TIMEOUT")
        
        decision = validate_action(case, payment, "RETRY_PAYMENT")
        
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.action, "RETRY_PAYMENT")
        self.assertIn("BANK_TIMEOUT", decision.reason)

    def test_temporary_bank_error_retry_is_allowed(self) -> None:
        """OPEN case + FAILED payment + TEMPORARY_BANK_ERROR + RETRY_PAYMENT -> allowed."""
        case = _recovery_case(status="OPEN")
        payment = _payment(failure_reason="TEMPORARY_BANK_ERROR")
        
        decision = validate_action(case, payment, "RETRY_PAYMENT")
        
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.action, "RETRY_PAYMENT")
        self.assertIn("TEMPORARY_BANK_ERROR", decision.reason)

    def test_network_error_retry_is_allowed(self) -> None:
        """OPEN case + FAILED payment + NETWORK_ERROR + RETRY_PAYMENT -> allowed."""
        case = _recovery_case(status="OPEN")
        payment = _payment(failure_reason="NETWORK_ERROR")
        
        decision = validate_action(case, payment, "RETRY_PAYMENT")
        
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.action, "RETRY_PAYMENT")
        self.assertIn("NETWORK_ERROR", decision.reason)

    def test_bank_server_down_retry_is_allowed(self) -> None:
        """OPEN case + FAILED payment + BANK_SERVER_DOWN + RETRY_PAYMENT -> allowed."""
        case = _recovery_case(status="OPEN")
        payment = _payment(failure_reason="BANK_SERVER_DOWN")
        
        decision = validate_action(case, payment, "RETRY_PAYMENT")
        
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.action, "RETRY_PAYMENT")
        self.assertIn("BANK_SERVER_DOWN", decision.reason)


class CustomerFailureRetryTests(unittest.TestCase):
    """Test that RETRY_PAYMENT is NOT allowed for customer failures."""

    def test_card_expired_retry_is_not_allowed(self) -> None:
        """OPEN case + FAILED payment + CARD_EXPIRED + RETRY_PAYMENT -> not allowed."""
        case = _recovery_case(status="OPEN")
        payment = _payment(failure_reason="CARD_EXPIRED")
        
        decision = validate_action(case, payment, "RETRY_PAYMENT")
        
        self.assertFalse(decision.allowed)
        self.assertIn("CARD_EXPIRED", decision.reason)

    def test_card_declined_retry_is_not_allowed(self) -> None:
        """OPEN case + FAILED payment + CARD_DECLINED + RETRY_PAYMENT -> not allowed."""
        case = _recovery_case(status="OPEN")
        payment = _payment(failure_reason="CARD_DECLINED")
        
        decision = validate_action(case, payment, "RETRY_PAYMENT")
        
        self.assertFalse(decision.allowed)
        self.assertIn("CARD_DECLINED", decision.reason)

    def test_insufficient_funds_retry_is_not_allowed(self) -> None:
        """OPEN case + FAILED payment + INSUFFICIENT_FUNDS + RETRY_PAYMENT -> not allowed."""
        case = _recovery_case(status="OPEN")
        payment = _payment(failure_reason="INSUFFICIENT_FUNDS")
        
        decision = validate_action(case, payment, "RETRY_PAYMENT")
        
        self.assertFalse(decision.allowed)
        self.assertIn("INSUFFICIENT_FUNDS", decision.reason)


class CaseStateConstraintTests(unittest.TestCase):
    """Test that non-OPEN recovery cases cannot execute actions."""

    def test_closed_case_action_is_not_allowed(self) -> None:
        """Non-OPEN recovery case + FAILED payment + temporary failure -> not allowed."""
        case = _recovery_case(status="CLOSED")
        payment = _payment(failure_reason="BANK_TIMEOUT")
        
        decision = validate_action(case, payment, "RETRY_PAYMENT")
        
        self.assertFalse(decision.allowed)
        self.assertIn("CLOSED", decision.reason)

    def test_resolved_case_action_is_not_allowed(self) -> None:
        """Non-OPEN recovery case (RESOLVED) -> not allowed."""
        case = _recovery_case(status="RESOLVED")
        payment = _payment(failure_reason="BANK_TIMEOUT")
        
        decision = validate_action(case, payment, "RETRY_PAYMENT")
        
        self.assertFalse(decision.allowed)
        self.assertIn("RESOLVED", decision.reason)


class PaymentStatusConstraintTests(unittest.TestCase):
    """Test that only FAILED payments can have recovery actions."""

    def test_success_payment_action_is_not_allowed(self) -> None:
        """OPEN case + SUCCESS payment -> not allowed."""
        case = _recovery_case(status="OPEN")
        payment = _payment(status="SUCCESS", failure_reason=None)
        
        decision = validate_action(case, payment, "RETRY_PAYMENT")
        
        self.assertFalse(decision.allowed)
        self.assertIn("SUCCESS", decision.reason)

    def test_pending_payment_action_is_not_allowed(self) -> None:
        """OPEN case + PENDING payment -> not allowed."""
        case = _recovery_case(status="OPEN")
        payment = _payment(status="PENDING", failure_reason=None)
        
        decision = validate_action(case, payment, "RETRY_PAYMENT")
        
        self.assertFalse(decision.allowed)
        self.assertIn("PENDING", decision.reason)


class UnsupportedActionTests(unittest.TestCase):
    """Test that unsupported actions are rejected."""

    def test_unsupported_action_is_not_allowed(self) -> None:
        """OPEN case + FAILED payment + unsupported action -> not allowed."""
        case = _recovery_case(status="OPEN")
        payment = _payment(failure_reason="BANK_TIMEOUT")
        
        decision = validate_action(case, payment, "UNKNOWN_ACTION")
        
        self.assertFalse(decision.allowed)
        self.assertIn("UNKNOWN_ACTION", decision.reason)


if __name__ == "__main__":
    unittest.main()
