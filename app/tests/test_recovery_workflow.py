import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.ai_agent import RecoveryRecommendation
from app.database import Base
from app.models import AuditEvent, Merchant, Payment, RecoveryAction, RecoveryCase
from app.recovery_workflow import process_recovery_batch, process_recovery_case


PAISE_PER_RUPEE = 100


class FakeProvider:
    def __init__(self, response: RecoveryRecommendation | None = None, fail: bool = False):
        self.response = response
        self.fail = fail

    def diagnose(self, context):
        if self.fail:
            raise RuntimeError("provider failed")

        if self.response is not None:
            return self.response

        if context.failure_reason in {"BANK_TIMEOUT", "TEMPORARY_BANK_ERROR", "NETWORK_ERROR", "BANK_SERVER_DOWN"}:
            return RecoveryRecommendation(
                diagnosis="Temporary issue suggests retry.",
                recommended_action="RETRY_PAYMENT",
                confidence=0.92,
                rationale="Temporary technical failure is usually retryable.",
            )

        if context.failure_reason in {"CARD_DECLINED", "CARD_EXPIRED", "INSUFFICIENT_FUNDS"}:
            return RecoveryRecommendation(
                diagnosis="Customer or payment-state failure requires escalation.",
                recommended_action="ESCALATE",
                confidence=0.98,
                rationale="This failure is not retryable under policy.",
            )

        raise ValueError("provider response missing")


class RecoveryWorkflowTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "test.db"
        self.db_url = f"sqlite:///{db_path}"
        self.engine = create_engine(self.db_url)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _create_merchant(self, session: Session, name: str = "Test Merchant") -> Merchant:
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
        payment = Payment(
            merchant_id=merchant_id,
            customer_id="CUST-0001",
            amount=amount_rupees * PAISE_PER_RUPEE,
            currency="INR",
            status=status,
            failure_reason=failure_reason,
        )
        session.add(payment)
        session.commit()
        return payment

    def _create_case(
        self,
        session: Session,
        payment_id: int,
        status: str = "OPEN",
        risk_score: int = 60,
        priority: str = "MEDIUM",
    ) -> RecoveryCase:
        case = RecoveryCase(
            payment_id=payment_id,
            status=status,
            risk_score=risk_score,
            priority=priority,
        )
        session.add(case)
        session.commit()
        return case


class RecoveryWorkflowCaseTests(RecoveryWorkflowTestBase):
    def test_retry_path_uses_ai_recommendation_and_policy(self) -> None:
        session = self.SessionLocal()
        merchant = self._create_merchant(session)
        payment = self._create_payment(session, merchant.id, failure_reason="BANK_TIMEOUT", amount_rupees=120)
        case = self._create_case(session, payment.id, priority="HIGH")

        provider = FakeProvider(
            RecoveryRecommendation(
                diagnosis="Temporary bank timeout",
                recommended_action="RETRY_PAYMENT",
                confidence=0.93,
                rationale="Temporary issue suggests retry.",
            )
        )

        result = process_recovery_case(session, case, provider=provider)

        self.assertEqual(result.recommended_action, "RETRY_PAYMENT")
        self.assertTrue(result.policy_allowed)
        self.assertEqual(result.execution_result, "RECOVERED")
        self.assertEqual(result.recovered_amount_paise, payment.amount)
        self.assertEqual(payment.status, "SUCCESS")
        self.assertEqual(case.status, "RESOLVED")

        action = session.query(RecoveryAction).filter(RecoveryAction.case_id == case.id).first()
        self.assertIsNotNone(action)
        self.assertEqual(action.status, "COMPLETED")

        audit_events = session.query(AuditEvent).filter(AuditEvent.case_id == case.id).all()
        self.assertTrue(any(event.event_type == "RECOVERY_SUCCEEDED" for event in audit_events))

    def test_escalation_path_blocks_retry_and_records_audit(self) -> None:
        session = self.SessionLocal()
        merchant = self._create_merchant(session)
        payment = self._create_payment(session, merchant.id, failure_reason="CARD_DECLINED", amount_rupees=200)
        case = self._create_case(session, payment.id)

        provider = FakeProvider(
            RecoveryRecommendation(
                diagnosis="Customer card declined",
                recommended_action="ESCALATE",
                confidence=0.99,
                rationale="Customer payment-state failure requires escalation.",
            )
        )

        result = process_recovery_case(session, case, provider=provider)

        self.assertEqual(result.recommended_action, "ESCALATE")
        self.assertFalse(result.policy_allowed)
        self.assertEqual(result.execution_result, "ESCALATED")
        self.assertEqual(result.recovered_amount_paise, 0)
        self.assertEqual(payment.status, "FAILED")
        self.assertEqual(case.status, "OPEN")

        audit_events = session.query(AuditEvent).filter(AuditEvent.case_id == case.id).all()
        self.assertTrue(any(event.event_type == "ESCALATION_RECORDED" for event in audit_events))

    def test_ai_failure_uses_deterministic_fallback(self) -> None:
        session = self.SessionLocal()
        merchant = self._create_merchant(session)
        payment = self._create_payment(session, merchant.id, failure_reason="BANK_TIMEOUT", amount_rupees=50)
        case = self._create_case(session, payment.id)

        provider = FakeProvider(fail=True)
        result = process_recovery_case(session, case, provider=provider)

        self.assertTrue(result.deterministic_fallback_used)
        self.assertEqual(result.recommended_action, "RETRY_PAYMENT")
        self.assertTrue(result.policy_allowed)
        self.assertEqual(result.execution_result, "RECOVERED")
        self.assertIn("Deterministic fallback", result.diagnosis)

    def test_policy_blocked_action_is_recorded_without_retry(self) -> None:
        session = self.SessionLocal()
        merchant = self._create_merchant(session)
        payment = self._create_payment(session, merchant.id, failure_reason="CARD_EXPIRED", amount_rupees=75)
        case = self._create_case(session, payment.id)

        provider = FakeProvider(
            RecoveryRecommendation(
                diagnosis="Card expired",
                recommended_action="RETRY_PAYMENT",
                confidence=0.8,
                rationale="Provider erroneously suggests retry.",
            )
        )

        result = process_recovery_case(session, case, provider=provider)

        self.assertFalse(result.policy_allowed)
        self.assertEqual(result.execution_result, "BLOCKED")
        self.assertEqual(result.recovered_amount_paise, 0)

        action = session.query(RecoveryAction).filter(RecoveryAction.case_id == case.id).first()
        self.assertIsNotNone(action)
        self.assertEqual(action.status, "BLOCKED")

    def test_resolved_case_is_not_retried(self) -> None:
        session = self.SessionLocal()
        merchant = self._create_merchant(session)
        payment = self._create_payment(session, merchant.id, failure_reason="BANK_TIMEOUT", amount_rupees=90)
        case = self._create_case(session, payment.id, status="RESOLVED")

        provider = FakeProvider(
            RecoveryRecommendation(
                diagnosis="Temporary issue",
                recommended_action="RETRY_PAYMENT",
                confidence=0.9,
                rationale="Would normally retry.",
            )
        )

        result = process_recovery_case(session, case, provider=provider)

        self.assertEqual(result.execution_result, "SKIPPED")
        self.assertFalse(result.policy_allowed)
        self.assertEqual(result.recovered_amount_paise, 0)
        self.assertEqual(payment.status, "FAILED")

    def test_repeated_execution_does_not_double_recover(self) -> None:
        session = self.SessionLocal()
        merchant = self._create_merchant(session)
        payment = self._create_payment(session, merchant.id, failure_reason="BANK_TIMEOUT", amount_rupees=120)
        case = self._create_case(session, payment.id)

        provider = FakeProvider(
            RecoveryRecommendation(
                diagnosis="Temporary bank timeout",
                recommended_action="RETRY_PAYMENT",
                confidence=0.9,
                rationale="Temporary issue suggests retry.",
            )
        )

        first = process_recovery_case(session, case, provider=provider)
        second = process_recovery_case(session, case, provider=provider)

        self.assertEqual(first.execution_result, "RECOVERED")
        self.assertEqual(second.execution_result, "SKIPPED")
        self.assertEqual(second.recovered_amount_paise, 0)


class RecoveryWorkflowBatchTests(RecoveryWorkflowTestBase):
    def test_batch_metrics_are_correct(self) -> None:
        session = self.SessionLocal()
        merchant = self._create_merchant(session)
        retry_payment = self._create_payment(session, merchant.id, failure_reason="BANK_TIMEOUT", amount_rupees=150)
        retry_case = self._create_case(session, retry_payment.id, priority="HIGH")

        escalate_payment = self._create_payment(session, merchant.id, failure_reason="CARD_DECLINED", amount_rupees=250)
        escalate_case = self._create_case(session, escalate_payment.id, priority="MEDIUM")

        resolved_payment = self._create_payment(session, merchant.id, status="SUCCESS", failure_reason=None, amount_rupees=100)
        self._create_case(session, resolved_payment.id, status="RESOLVED")

        provider = FakeProvider()

        metrics = process_recovery_batch(session, provider=provider)

        self.assertEqual(metrics["total_cases_processed"], 2)
        self.assertEqual(metrics["total_revenue_at_risk_paise"], (150 + 250) * PAISE_PER_RUPEE)
        self.assertEqual(metrics["recovered_revenue_paise"], 150 * PAISE_PER_RUPEE)
        self.assertEqual(metrics["number_recovered"], 1)
        self.assertEqual(metrics["number_escalated"], 1)
        self.assertEqual(metrics["number_blocked_by_policy"], 0)
        self.assertEqual(metrics["recovery_rate"], 37)


if __name__ == "__main__":
    unittest.main()
