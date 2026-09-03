import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import AuditEvent, Merchant, Payment, RecoveryAction, RecoveryCase
from app.recovery_executor import run_recovery_batch


class RecoveryExecutorTestBase(unittest.TestCase):
    """Base class for recovery executor tests with isolated in-memory database."""
    
    def setUp(self) -> None:
        """Create a temporary SQLite database for testing."""
        self.temp_dir = TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test.db"
        self.db_url = f"sqlite:///{db_path}"
        
        self.engine = create_engine(self.db_url)
        Base.metadata.create_all(self.engine)
        
        self.SessionLocal = sessionmaker(bind=self.engine)
    
    def tearDown(self) -> None:
        """Clean up database."""
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temp_dir.cleanup()
    
    def _create_merchant(self, session: Session, name: str = "Test Merchant") -> Merchant:
        """Helper to create a merchant."""
        merchant = Merchant(name=name, currency="INR")
        session.add(merchant)
        session.commit()
        return merchant
    
    def _create_payment(
        self,
        session: Session,
        merchant_id: int,
        status: str = "FAILED",
        failure_reason: str | None = "BANK_TIMEOUT",
        amount_rupees: int = 1000,
    ) -> Payment:
        """Helper to create a payment."""
        payment = Payment(
            merchant_id=merchant_id,
            customer_id="CUST-0001",
            amount=amount_rupees * 100,  # Convert rupees to paise
            currency="INR",
            status=status,
            failure_reason=failure_reason,
        )
        session.add(payment)
        session.commit()
        return payment
    
    def _create_recovery_case(
        self,
        session: Session,
        payment_id: int,
        status: str = "OPEN",
        risk_score: int = 60,
        priority: str = "MEDIUM",
    ) -> RecoveryCase:
        """Helper to create a recovery case."""
        case = RecoveryCase(
            payment_id=payment_id,
            status=status,
            risk_score=risk_score,
            priority=priority,
        )
        session.add(case)
        session.commit()
        return case


class TemporaryFailureRecoveryTests(RecoveryExecutorTestBase):
    """Test recovery of temporary failures."""
    
    def test_bank_timeout_recovers(self) -> None:
        """BANK_TIMEOUT failure recovers successfully."""
        session = self.SessionLocal()
        
        merchant = self._create_merchant(session)
        payment = self._create_payment(
            session, merchant.id, failure_reason="BANK_TIMEOUT", amount_rupees=1000
        )
        case = self._create_recovery_case(session, payment.id)
        
        metrics = run_recovery_batch(session)
        
        # Verify recovery occurred
        self.assertEqual(metrics["cases_processed"], 1)
        self.assertEqual(metrics["actions_attempted"], 1)
        self.assertEqual(metrics["actions_blocked"], 0)
        self.assertEqual(metrics["already_attempted"], 0)
        self.assertEqual(metrics["payments_recovered"], 1)
        self.assertEqual(metrics["recovered_revenue_paise"], 1000 * 100)
        
        # Verify payment status changed
        session.refresh(payment)
        self.assertEqual(payment.status, "SUCCESS")
        
        # Verify case status changed
        session.refresh(case)
        self.assertEqual(case.status, "RESOLVED")
        
        # Verify recovery action was created
        recovery_action = session.query(RecoveryAction).filter(
            RecoveryAction.case_id == case.id
        ).first()
        self.assertIsNotNone(recovery_action)
        self.assertEqual(recovery_action.action_type, "RETRY_PAYMENT")
        self.assertEqual(recovery_action.status, "COMPLETED")
        self.assertIsNotNone(recovery_action.attempted_at)
        self.assertIsNotNone(recovery_action.completed_at)
        
        # Verify audit event was created
        audit_events = session.query(AuditEvent).filter(
            AuditEvent.case_id == case.id
        ).all()
        self.assertGreater(len(audit_events), 0)
        recovery_events = [e for e in audit_events if e.event_type == "RECOVERY_SUCCEEDED"]
        self.assertEqual(len(recovery_events), 1)
        self.assertIn("100000 paise", recovery_events[0].details)
        
        session.close()
    
    def test_temporary_bank_error_recovers(self) -> None:
        """TEMPORARY_BANK_ERROR failure recovers successfully."""
        session = self.SessionLocal()
        
        merchant = self._create_merchant(session)
        payment = self._create_payment(
            session, merchant.id, failure_reason="TEMPORARY_BANK_ERROR", amount_rupees=500
        )
        case = self._create_recovery_case(session, payment.id)
        
        metrics = run_recovery_batch(session)
        
        self.assertEqual(metrics["payments_recovered"], 1)
        self.assertEqual(metrics["recovered_revenue_paise"], 500 * 100)
        
        session.refresh(payment)
        self.assertEqual(payment.status, "SUCCESS")
        
        session.close()
    
    def test_network_error_recovers(self) -> None:
        """NETWORK_ERROR failure recovers successfully."""
        session = self.SessionLocal()
        
        merchant = self._create_merchant(session)
        payment = self._create_payment(
            session, merchant.id, failure_reason="NETWORK_ERROR", amount_rupees=2500
        )
        case = self._create_recovery_case(session, payment.id)
        
        metrics = run_recovery_batch(session)
        
        self.assertEqual(metrics["payments_recovered"], 1)
        self.assertEqual(metrics["recovered_revenue_paise"], 2500 * 100)
        
        session.close()
    
    def test_bank_server_down_recovers(self) -> None:
        """BANK_SERVER_DOWN failure recovers successfully."""
        session = self.SessionLocal()
        
        merchant = self._create_merchant(session)
        payment = self._create_payment(
            session, merchant.id, failure_reason="BANK_SERVER_DOWN", amount_rupees=5000
        )
        case = self._create_recovery_case(session, payment.id)
        
        metrics = run_recovery_batch(session)
        
        self.assertEqual(metrics["payments_recovered"], 1)
        self.assertEqual(metrics["recovered_revenue_paise"], 5000 * 100)
        
        session.close()


class CustomerFailureBlockingTests(RecoveryExecutorTestBase):
    """Test that customer-side failures are blocked."""
    
    def test_card_expired_is_blocked(self) -> None:
        """CARD_EXPIRED failure is blocked by policy."""
        session = self.SessionLocal()
        
        merchant = self._create_merchant(session)
        payment = self._create_payment(
            session, merchant.id, failure_reason="CARD_EXPIRED", amount_rupees=1000
        )
        case = self._create_recovery_case(session, payment.id)
        
        metrics = run_recovery_batch(session)
        
        self.assertEqual(metrics["cases_processed"], 1)
        self.assertEqual(metrics["actions_blocked"], 1)
        self.assertEqual(metrics["actions_attempted"], 0)
        self.assertEqual(metrics["payments_recovered"], 0)
        self.assertEqual(metrics["recovered_revenue_paise"], 0)
        
        # Verify payment status unchanged
        session.refresh(payment)
        self.assertEqual(payment.status, "FAILED")
        
        # Verify case status unchanged
        session.refresh(case)
        self.assertEqual(case.status, "OPEN")
        
        # Verify blocked action was created
        recovery_action = session.query(RecoveryAction).filter(
            RecoveryAction.case_id == case.id
        ).first()
        self.assertIsNotNone(recovery_action)
        self.assertEqual(recovery_action.status, "BLOCKED")
        
        # Verify audit event was created
        audit_events = session.query(AuditEvent).filter(
            AuditEvent.case_id == case.id
        ).all()
        blocked_events = [e for e in audit_events if e.event_type == "RETRY_BLOCKED_POLICY"]
        self.assertEqual(len(blocked_events), 1)
        self.assertIn("CARD_EXPIRED", blocked_events[0].details)
        
        session.close()
    
    def test_card_declined_is_blocked(self) -> None:
        """CARD_DECLINED failure is blocked by policy."""
        session = self.SessionLocal()
        
        merchant = self._create_merchant(session)
        payment = self._create_payment(
            session, merchant.id, failure_reason="CARD_DECLINED", amount_rupees=1000
        )
        case = self._create_recovery_case(session, payment.id)
        
        metrics = run_recovery_batch(session)
        
        self.assertEqual(metrics["actions_blocked"], 1)
        self.assertEqual(metrics["actions_attempted"], 0)
        
        session.close()
    
    def test_insufficient_funds_is_blocked(self) -> None:
        """INSUFFICIENT_FUNDS failure is blocked by policy."""
        session = self.SessionLocal()
        
        merchant = self._create_merchant(session)
        payment = self._create_payment(
            session, merchant.id, failure_reason="INSUFFICIENT_FUNDS", amount_rupees=1000
        )
        case = self._create_recovery_case(session, payment.id)
        
        metrics = run_recovery_batch(session)
        
        self.assertEqual(metrics["actions_blocked"], 1)
        self.assertEqual(metrics["actions_attempted"], 0)
        
        session.close()


class CaseStateConstraintTests(RecoveryExecutorTestBase):
    """Test that non-OPEN cases are not processed."""
    
    def test_closed_case_not_processed(self) -> None:
        """CLOSED case is not processed."""
        session = self.SessionLocal()
        
        merchant = self._create_merchant(session)
        payment = self._create_payment(
            session, merchant.id, failure_reason="BANK_TIMEOUT", amount_rupees=1000
        )
        case = self._create_recovery_case(session, payment.id, status="CLOSED")
        
        metrics = run_recovery_batch(session)
        
        self.assertEqual(metrics["cases_processed"], 0)
        self.assertEqual(metrics["actions_attempted"], 0)
        self.assertEqual(metrics["payments_recovered"], 0)
        
        session.close()


class IdempotencyTests(RecoveryExecutorTestBase):
    """Test idempotency: no duplicate recovery on second batch run."""
    
    def test_already_attempted_prevented(self) -> None:
        """Second batch run prevents duplicate recovery."""
        session = self.SessionLocal()
        
        merchant = self._create_merchant(session)
        payment = self._create_payment(
            session, merchant.id, failure_reason="BANK_TIMEOUT", amount_rupees=1000
        )
        case = self._create_recovery_case(session, payment.id)
        
        # First run: recovery succeeds
        metrics1 = run_recovery_batch(session)
        self.assertEqual(metrics1["actions_attempted"], 1)
        self.assertEqual(metrics1["payments_recovered"], 1)
        self.assertEqual(metrics1["recovered_revenue_paise"], 100000)
        
        # Verify payment is now SUCCESS
        session.refresh(payment)
        self.assertEqual(payment.status, "SUCCESS")
        
        # Reset case to OPEN to simulate batch re-run scenario
        session.refresh(case)
        case.status = "OPEN"
        payment.status = "FAILED"  # Reset payment status for idempotency test
        session.commit()
        
        # Second run: should prevent retry due to existing action
        metrics2 = run_recovery_batch(session)
        self.assertEqual(metrics2["cases_processed"], 1)
        self.assertEqual(metrics2["already_attempted"], 1)
        self.assertEqual(metrics2["actions_attempted"], 0)
        self.assertEqual(metrics2["payments_recovered"], 0)
        self.assertEqual(metrics2["recovered_revenue_paise"], 0)
        
        # Verify no additional recovery action was created
        recovery_actions = session.query(RecoveryAction).filter(
            RecoveryAction.case_id == case.id
        ).all()
        self.assertEqual(len(recovery_actions), 1)  # Only the original one
        
        # Verify idempotency prevention audit event was created
        audit_events = session.query(AuditEvent).filter(
            AuditEvent.case_id == case.id,
            AuditEvent.event_type == "RETRY_PREVENTED_IDEMPOTENCY",
        ).all()
        self.assertEqual(len(audit_events), 1)
        
        session.close()
    
    def test_blocked_action_counts_toward_idempotency(self) -> None:
        """Blocked action counts toward idempotency limit."""
        session = self.SessionLocal()
        
        merchant = self._create_merchant(session)
        payment = self._create_payment(
            session, merchant.id, failure_reason="CARD_EXPIRED", amount_rupees=1000
        )
        case = self._create_recovery_case(session, payment.id)
        
        # First run: action is blocked
        metrics1 = run_recovery_batch(session)
        self.assertEqual(metrics1["actions_blocked"], 1)
        
        # Reset case to OPEN for second run
        session.refresh(case)
        case.status = "OPEN"
        session.commit()
        
        # Second run: should prevent retry due to existing (blocked) action
        metrics2 = run_recovery_batch(session)
        self.assertEqual(metrics2["already_attempted"], 1)
        self.assertEqual(metrics2["actions_blocked"], 0)
        
        session.close()


class MultipleCasesTests(RecoveryExecutorTestBase):
    """Test batch processing with multiple cases."""
    
    def test_multiple_cases_mixed_outcomes(self) -> None:
        """Multiple cases with mixed temporary/customer failures."""
        session = self.SessionLocal()
        
        merchant = self._create_merchant(session)
        
        # Create temporary failure cases (should recover)
        payment1 = self._create_payment(
            session, merchant.id, failure_reason="BANK_TIMEOUT", amount_rupees=1000
        )
        case1 = self._create_recovery_case(session, payment1.id)
        
        payment2 = self._create_payment(
            session, merchant.id, failure_reason="NETWORK_ERROR", amount_rupees=2000
        )
        case2 = self._create_recovery_case(session, payment2.id)
        
        # Create customer failure cases (should be blocked)
        payment3 = self._create_payment(
            session, merchant.id, failure_reason="CARD_EXPIRED", amount_rupees=500
        )
        case3 = self._create_recovery_case(session, payment3.id)
        
        payment4 = self._create_payment(
            session, merchant.id, failure_reason="INSUFFICIENT_FUNDS", amount_rupees=1500
        )
        case4 = self._create_recovery_case(session, payment4.id)
        
        metrics = run_recovery_batch(session)
        
        # Verify metrics
        self.assertEqual(metrics["cases_processed"], 4)
        self.assertEqual(metrics["actions_attempted"], 2)
        self.assertEqual(metrics["actions_blocked"], 2)
        self.assertEqual(metrics["payments_recovered"], 2)
        # 1000 + 2000 = 3000 rupees = 300000 paise
        self.assertEqual(metrics["recovered_revenue_paise"], 300000)
        
        # Verify payment statuses
        session.refresh(payment1)
        session.refresh(payment2)
        session.refresh(payment3)
        session.refresh(payment4)
        self.assertEqual(payment1.status, "SUCCESS")
        self.assertEqual(payment2.status, "SUCCESS")
        self.assertEqual(payment3.status, "FAILED")
        self.assertEqual(payment4.status, "FAILED")
        
        session.close()


class AuditEventTests(RecoveryExecutorTestBase):
    """Test audit event creation."""
    
    def test_all_outcomes_have_audit_events(self) -> None:
        """Every recovery outcome has an audit event."""
        session = self.SessionLocal()
        
        merchant = self._create_merchant(session)
        
        # Successful recovery
        payment1 = self._create_payment(
            session, merchant.id, failure_reason="BANK_TIMEOUT", amount_rupees=1000
        )
        case1 = self._create_recovery_case(session, payment1.id)
        
        # Blocked recovery
        payment2 = self._create_payment(
            session, merchant.id, failure_reason="CARD_EXPIRED", amount_rupees=1000
        )
        case2 = self._create_recovery_case(session, payment2.id)
        
        run_recovery_batch(session)
        
        # Check case1 audit events (successful)
        events1 = session.query(AuditEvent).filter(
            AuditEvent.case_id == case1.id
        ).all()
        self.assertGreater(len(events1), 0)
        recovery_events1 = [e for e in events1 if e.event_type == "RECOVERY_SUCCEEDED"]
        self.assertEqual(len(recovery_events1), 1)
        
        # Check case2 audit events (blocked)
        events2 = session.query(AuditEvent).filter(
            AuditEvent.case_id == case2.id
        ).all()
        self.assertGreater(len(events2), 0)
        blocked_events2 = [e for e in events2 if e.event_type == "RETRY_BLOCKED_POLICY"]
        self.assertEqual(len(blocked_events2), 1)
        
        session.close()
    
    def test_audit_event_details_are_meaningful(self) -> None:
        """Audit event details contain useful information."""
        session = self.SessionLocal()
        
        merchant = self._create_merchant(session)
        payment = self._create_payment(
            session, merchant.id, failure_reason="BANK_TIMEOUT", amount_rupees=1000
        )
        case = self._create_recovery_case(session, payment.id)
        
        run_recovery_batch(session)
        
        audit_events = session.query(AuditEvent).filter(
            AuditEvent.case_id == case.id
        ).all()
        
        recovery_event = next(
            (e for e in audit_events if e.event_type == "RECOVERY_SUCCEEDED"),
            None,
        )
        self.assertIsNotNone(recovery_event)
        self.assertIn("payment", recovery_event.details.lower())
        self.assertIn("100000 paise", recovery_event.details)
        self.assertIn("BANK_TIMEOUT", recovery_event.details)
        
        session.close()


class PaiseHandlingTests(RecoveryExecutorTestBase):
    """Test that all monetary calculations use integer paise."""
    
    def test_recovered_revenue_is_in_paise(self) -> None:
        """Recovered revenue metric uses integer paise."""
        session = self.SessionLocal()
        
        merchant = self._create_merchant(session)
        payment = self._create_payment(
            session, merchant.id, failure_reason="BANK_TIMEOUT", amount_rupees=999
        )
        case = self._create_recovery_case(session, payment.id)
        
        metrics = run_recovery_batch(session)
        
        # 999 rupees = 99900 paise
        self.assertEqual(metrics["recovered_revenue_paise"], 99900)
        self.assertIsInstance(metrics["recovered_revenue_paise"], int)
        
        session.close()


if __name__ == "__main__":
    unittest.main()
