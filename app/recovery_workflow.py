from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai_agent import RecoveryContext, RecoveryRecommendation, diagnose_with_fallback
from app.models import AuditEvent, Payment, RecoveryAction, RecoveryCase
from app.policy_engine import validate_action


@dataclass(frozen=True)
class RecoveryCaseResult:
    """Structured outcome for a single recovery case."""

    case_id: int
    payment_id: int
    risk_score: int | None
    priority: str | None
    diagnosis: str
    recommended_action: str
    confidence: float
    rationale: str
    deterministic_fallback_used: bool
    policy_allowed: bool
    execution_result: str
    recovered_amount_paise: int = 0
    policy_reason: str | None = None

    def __getitem__(self, key: str):
        return getattr(self, key)


@dataclass(frozen=True)
class BatchRecoveryMetrics:
    """Aggregate metrics for a batch of recovery cases."""

    total_cases_processed: int
    total_revenue_at_risk_paise: int
    recovered_revenue_paise: int
    number_recovered: int
    number_escalated: int
    number_blocked_by_policy: int
    number_using_deterministic_fallback: int
    recovery_rate: int

    def __getitem__(self, key: str):
        return getattr(self, key)


def _build_context(case: RecoveryCase, payment: Payment) -> RecoveryContext:
    return RecoveryContext(
        payment_id=payment.id,
        amount_paise=payment.amount,
        currency=payment.currency,
        payment_status=payment.status,
        failure_reason=payment.failure_reason,
        risk_score=case.risk_score,
        priority=case.priority,
    )


def _record_audit(session: Session, case_id: int, event_type: str, actor: str, details: str) -> None:
    session.add(
        AuditEvent(
            case_id=case_id,
            event_type=event_type,
            actor=actor,
            details=details,
        )
    )


def process_recovery_case(
    session: Session,
    case: RecoveryCase,
    provider=None,
    fallback_provider=None,
) -> RecoveryCaseResult:
    """Process one open recovery case through the AI, policy, and executor flow."""
    payment = session.scalar(select(Payment).where(Payment.id == case.payment_id))

    if payment is None:
        return RecoveryCaseResult(
            case_id=case.id,
            payment_id=case.payment_id,
            risk_score=case.risk_score,
            priority=case.priority,
            diagnosis="Payment not found for recovery case.",
            recommended_action="ESCALATE",
            confidence=0.0,
            rationale="No payment was found for this case, so no automated recovery is possible.",
            deterministic_fallback_used=False,
            policy_allowed=False,
            execution_result="SKIPPED",
            recovered_amount_paise=0,
            policy_reason="Payment is missing.",
        )

    if case.status != "OPEN":
        _record_audit(
            session,
            case.id,
            "CASE_NOT_OPEN",
            "workflow",
            f"Case {case.id} is not OPEN; no recovery workflow action executed.",
        )
        session.commit()
        return RecoveryCaseResult(
            case_id=case.id,
            payment_id=case.payment_id,
            risk_score=case.risk_score,
            priority=case.priority,
            diagnosis=f"Recovery case status is {case.status!r}.",
            recommended_action="ESCALATE",
            confidence=0.0,
            rationale="The case is already resolved or closed; no automatic retry is allowed.",
            deterministic_fallback_used=False,
            policy_allowed=False,
            execution_result="SKIPPED",
            recovered_amount_paise=0,
            policy_reason=f"Case status is {case.status!r}.",
        )

    existing_retry_action = session.scalar(
        select(RecoveryAction).where(
            RecoveryAction.case_id == case.id,
            RecoveryAction.action_type == "RETRY_PAYMENT",
        )
    )
    if existing_retry_action is not None:
        _record_audit(
            session,
            case.id,
            "RETRY_PREVENTED_IDEMPOTENCY",
            "workflow",
            "Retry prevented to enforce idempotency: a retry action already exists for this case.",
        )
        session.commit()
        return RecoveryCaseResult(
            case_id=case.id,
            payment_id=case.payment_id,
            risk_score=case.risk_score,
            priority=case.priority,
            diagnosis="Retry already attempted for this case.",
            recommended_action="ESCALATE",
            confidence=0.0,
            rationale="Idempotency protection prevents duplicate retries for an already-processed case.",
            deterministic_fallback_used=False,
            policy_allowed=False,
            execution_result="SKIPPED",
            recovered_amount_paise=0,
            policy_reason="Existing retry action detected.",
        )

    context = _build_context(case, payment)
    recommendation = diagnose_with_fallback(
        context,
        provider=provider,
        fallback_provider=fallback_provider,
    )

    deterministic_fallback_used = (
        recommendation.diagnosis.startswith("Deterministic fallback:")
        or "Deterministic fallback" in recommendation.rationale
    )

    decision = validate_action(case, payment, recommendation.recommended_action)
    now = datetime.now(timezone.utc)

    if recommendation.recommended_action == "ESCALATE" or not decision.allowed:
        if recommendation.recommended_action == "ESCALATE":
            action_type = "ESCALATE"
            status = "RECORDED"
            event_type = "ESCALATION_RECORDED"
            reason = (
                "AI recommended ESCALATE; automatic RETRY_PAYMENT is not authorized by policy. "
                f"Policy reason: {decision.reason}"
            )
            execution_result = "ESCALATED"
        else:
            action_type = "RETRY_PAYMENT"
            status = "BLOCKED"
            event_type = "RETRY_BLOCKED_POLICY"
            reason = decision.reason
            execution_result = "BLOCKED"

        session.add(
            RecoveryAction(
                case_id=case.id,
                action_type=action_type,
                status=status,
                attempted_at=now,
            )
        )
        _record_audit(
            session,
            case.id,
            event_type,
            "policy_engine",
            reason,
        )
        session.commit()
        return RecoveryCaseResult(
            case_id=case.id,
            payment_id=case.payment_id,
            risk_score=case.risk_score,
            priority=case.priority,
            diagnosis=recommendation.diagnosis,
            recommended_action=recommendation.recommended_action,
            confidence=recommendation.confidence,
            rationale=recommendation.rationale,
            deterministic_fallback_used=deterministic_fallback_used,
            policy_allowed=decision.allowed,
            execution_result=execution_result,
            recovered_amount_paise=0,
            policy_reason=decision.reason,
        )

    payment.status = "SUCCESS"
    case.status = "RESOLVED"
    case.updated_at = now

    session.add(
        RecoveryAction(
            case_id=case.id,
            action_type="RETRY_PAYMENT",
            status="COMPLETED",
            attempted_at=now,
            completed_at=now,
        )
    )
    _record_audit(
        session,
        case.id,
        "RECOVERY_SUCCEEDED",
        "recovery_executor",
        (
            f"Test-mode recovery succeeded for payment {payment.id}. "
            f"Recovered amount: {payment.amount} paise. "
            f"Failure reason was: {payment.failure_reason!r}"
        ),
    )
    session.commit()

    return RecoveryCaseResult(
        case_id=case.id,
        payment_id=case.payment_id,
        risk_score=case.risk_score,
        priority=case.priority,
        diagnosis=recommendation.diagnosis,
        recommended_action=recommendation.recommended_action,
        confidence=recommendation.confidence,
        rationale=recommendation.rationale,
        deterministic_fallback_used=deterministic_fallback_used,
        policy_allowed=True,
        execution_result="RECOVERED",
        recovered_amount_paise=payment.amount,
        policy_reason=decision.reason,
    )


def process_recovery_batch(
    session: Session,
    provider=None,
    fallback_provider=None,
) -> BatchRecoveryMetrics:
    """Process all OPEN recovery cases and return aggregate metrics."""
    open_cases = session.scalars(
        select(RecoveryCase)
        .where(RecoveryCase.status == "OPEN")
        .order_by(RecoveryCase.id)
    ).all()

    total_revenue_at_risk_paise = 0
    recovered_revenue_paise = 0
    number_recovered = 0
    number_escalated = 0
    number_blocked_by_policy = 0
    number_using_deterministic_fallback = 0

    for case in open_cases:
        payment = session.scalar(select(Payment).where(Payment.id == case.payment_id))
        if payment is not None:
            total_revenue_at_risk_paise += payment.amount

        result = process_recovery_case(session, case, provider=provider, fallback_provider=fallback_provider)

        if result.execution_result == "RECOVERED":
            number_recovered += 1
            recovered_revenue_paise += result.recovered_amount_paise
        elif result.execution_result == "ESCALATED":
            number_escalated += 1
        elif result.execution_result == "BLOCKED":
            number_blocked_by_policy += 1

        if result.deterministic_fallback_used:
            number_using_deterministic_fallback += 1

    if total_revenue_at_risk_paise == 0:
        recovery_rate = 0
    else:
        recovery_rate = int((recovered_revenue_paise * 100) // total_revenue_at_risk_paise)

    return BatchRecoveryMetrics(
        total_cases_processed=len(open_cases),
        total_revenue_at_risk_paise=total_revenue_at_risk_paise,
        recovered_revenue_paise=recovered_revenue_paise,
        number_recovered=number_recovered,
        number_escalated=number_escalated,
        number_blocked_by_policy=number_blocked_by_policy,
        number_using_deterministic_fallback=number_using_deterministic_fallback,
        recovery_rate=recovery_rate,
    )
