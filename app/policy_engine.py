from dataclasses import dataclass

from app.models import Payment, RecoveryCase


TEMPORARY_FAILURE_REASONS = {
    "BANK_TIMEOUT",
    "TEMPORARY_BANK_ERROR",
    "NETWORK_ERROR",
    "BANK_SERVER_DOWN",
}

AUTOMATIC_RETRY_ACTION = "RETRY_PAYMENT"


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    action: str
    reason: str


def validate_action(
    case: RecoveryCase,
    payment: Payment,
    action: str,
) -> PolicyDecision:
    """
    Deterministic policy gate for recovery actions.
    
    Validates that a proposed recovery action is allowed based on:
    - Recovery case state
    - Payment status and failure reason
    - Action type
    
    Args:
        case: The recovery case being evaluated.
        payment: The payment associated with the case.
        action: The proposed action type.
    
    Returns:
        PolicyDecision with allowed flag, action, and reason.
    """
    
    # Rule 1: Case must be OPEN
    if case.status != "OPEN":
        return PolicyDecision(
            allowed=False,
            action=action,
            reason=f"Recovery case status is {case.status!r}, not OPEN. No actions allowed.",
        )
    
    # Rule 2: Payment must be FAILED
    if payment.status != "FAILED":
        return PolicyDecision(
            allowed=False,
            action=action,
            reason=f"Payment status is {payment.status!r}, not FAILED. No recovery action applies.",
        )
    
    # Rule 3: Action must be supported (RETRY_PAYMENT)
    if action != AUTOMATIC_RETRY_ACTION:
        return PolicyDecision(
            allowed=False,
            action=action,
            reason=f"Action {action!r} is not supported by the policy engine.",
        )
    
    # Rule 4: Failure reason must be temporary (retryable)
    if payment.failure_reason not in TEMPORARY_FAILURE_REASONS:
        return PolicyDecision(
            allowed=False,
            action=action,
            reason=(
                f"Failure reason {payment.failure_reason!r} is not temporary. "
                f"Automatic retry is not allowed for this failure type."
            ),
        )
    
    # Rule 5: All checks passed; action is allowed
    return PolicyDecision(
        allowed=True,
        action=AUTOMATIC_RETRY_ACTION,
        reason=(
            f"Automatic retry is allowed: failure reason {payment.failure_reason!r} "
            f"is temporary and retryable."
        ),
    )
