from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditEvent, Payment, RecoveryAction, RecoveryCase
from app.policy_engine import validate_action


def run_recovery_batch(session: Session) -> dict:
    """
    Execute deterministic, test-mode recovery for OPEN recovery cases.
    
    Processes each OPEN case in ID order:
    1. Checks for existing RETRY_PAYMENT action (idempotency).
    2. Validates action with policy engine.
    3. Creates RecoveryAction and AuditEvent records.
    4. On allowed action: simulates successful recovery without external calls.
    5. On blocked action: records policy rejection.
    
    Args:
        session: SQLAlchemy session for database access.
    
    Returns:
        Dictionary with metrics:
        - cases_processed: OPEN cases examined
        - actions_attempted: Successful test-mode recoveries
        - actions_blocked: Policy-blocked recovery attempts
        - already_attempted: Cases with existing retry actions
        - payments_recovered: Successful recoveries
        - recovered_revenue_paise: Total recovered (integer paise)
    """
    
    metrics = {
        "cases_processed": 0,
        "actions_attempted": 0,
        "actions_blocked": 0,
        "already_attempted": 0,
        "payments_recovered": 0,
        "recovered_revenue_paise": 0,
    }
    
    try:
        # Load all OPEN cases in deterministic order
        open_cases = session.scalars(
            select(RecoveryCase)
            .where(RecoveryCase.status == "OPEN")
            .order_by(RecoveryCase.id)
        ).all()
        
        metrics["cases_processed"] = len(open_cases)
        
        for case in open_cases:
            # Load the associated payment
            payment = session.scalar(
                select(Payment).where(Payment.id == case.payment_id)
            )
            if payment is None:
                # This should not happen in normal operation, but handle gracefully
                continue
            
            # Check for idempotency: has this case already attempted a retry?
            existing_retry_action = session.scalar(
                select(RecoveryAction).where(
                    RecoveryAction.case_id == case.id,
                    RecoveryAction.action_type == "RETRY_PAYMENT",
                )
            )
            
            if existing_retry_action is not None:
                # Already attempted; do not retry
                metrics["already_attempted"] += 1
                
                # Document idempotency prevention in audit trail
                audit_event = AuditEvent(
                    case_id=case.id,
                    event_type="RETRY_PREVENTED_IDEMPOTENCY",
                    actor="recovery_executor",
                    details=(
                        f"Case {case.id} already has a RETRY_PAYMENT action "
                        f"(status: {existing_retry_action.status!r}). "
                        f"Retry prevented to enforce idempotency constraint."
                    ),
                )
                session.add(audit_event)
                continue
            
            # Validate the proposed action with the policy engine
            decision = validate_action(case, payment, "RETRY_PAYMENT")
            
            now = datetime.now(timezone.utc)
            
            if not decision.allowed:
                # Policy engine blocked the action
                metrics["actions_blocked"] += 1
                
                # Create a BLOCKED recovery action
                blocked_action = RecoveryAction(
                    case_id=case.id,
                    action_type="RETRY_PAYMENT",
                    status="BLOCKED",
                    attempted_at=now,
                )
                session.add(blocked_action)
                
                # Create audit event documenting the policy rejection
                audit_event = AuditEvent(
                    case_id=case.id,
                    event_type="RETRY_BLOCKED_POLICY",
                    actor="policy_engine",
                    details=(
                        f"RETRY_PAYMENT blocked by policy: {decision.reason} "
                        f"Payment failure reason: {payment.failure_reason!r}"
                    ),
                )
                session.add(audit_event)
                
            else:
                # Policy engine allowed the action; proceed with test-mode recovery
                metrics["actions_attempted"] += 1
                metrics["payments_recovered"] += 1
                metrics["recovered_revenue_paise"] += payment.amount
                
                # Create a COMPLETED recovery action
                completed_action = RecoveryAction(
                    case_id=case.id,
                    action_type="RETRY_PAYMENT",
                    status="COMPLETED",
                    attempted_at=now,
                    completed_at=now,
                )
                session.add(completed_action)
                
                # Simulate successful recovery: update payment and case status
                payment.status = "SUCCESS"
                case.status = "RESOLVED"
                case.updated_at = now
                
                # Create audit event documenting successful recovery
                audit_event = AuditEvent(
                    case_id=case.id,
                    event_type="RECOVERY_SUCCEEDED",
                    actor="recovery_executor",
                    details=(
                        f"Test-mode recovery succeeded for payment {payment.id}. "
                        f"Recovered amount: {payment.amount} paise. "
                        f"Failure reason was: {payment.failure_reason!r}"
                    ),
                )
                session.add(audit_event)
        
        # Commit all changes
        session.commit()
        
    except Exception as exc:
        # Rollback on any unexpected error
        session.rollback()
        raise
    
    return metrics
