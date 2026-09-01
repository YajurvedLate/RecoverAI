from dataclasses import dataclass

from app.models import Payment

FAILURE_REASON_BASE_SCORES: dict[str, int] = {
    "BANK_TIMEOUT": 55,
    "TEMPORARY_BANK_ERROR": 55,
    "NETWORK_ERROR": 50,
    "BANK_SERVER_DOWN": 55,
    "INSUFFICIENT_FUNDS": 25,
    "CARD_EXPIRED": 15,
    "CARD_DECLINED": 20,
}

PAISE_PER_RUPEE = 100
AMOUNT_LT_1000_PAISE = 1_000 * PAISE_PER_RUPEE
AMOUNT_LT_5000_PAISE = 5_000 * PAISE_PER_RUPEE
AMOUNT_LT_10000_PAISE = 10_000 * PAISE_PER_RUPEE

MAX_SCORE = 100


class PaymentNotFailedError(ValueError):
    pass


class UnsupportedFailureReasonError(ValueError):
    pass


@dataclass(frozen=True)
class RecoveryOpportunity:
    score: int
    priority: str
    reason: str


def _amount_contribution(amount_paise: int) -> tuple[int, str]:
    if amount_paise < AMOUNT_LT_1000_PAISE:
        return 5, "below ₹1,000"
    if amount_paise < AMOUNT_LT_5000_PAISE:
        return 10, "₹1,000–₹4,999"
    if amount_paise < AMOUNT_LT_10000_PAISE:
        return 20, "₹5,000–₹9,999"
    return 30, "₹10,000 or more"


def _priority_for_score(score: int) -> str:
    if score >= 80:
        return "HIGH"
    if score >= 50:
        return "MEDIUM"
    return "LOW"


def score_payment(payment: Payment) -> RecoveryOpportunity:
    if payment.status != "FAILED":
        raise PaymentNotFailedError(
            f"Risk engine scores failed payments only; status was {payment.status!r}."
        )

    failure_reason = payment.failure_reason
    if failure_reason not in FAILURE_REASON_BASE_SCORES:
        raise UnsupportedFailureReasonError(
            f"Unsupported failure reason: {failure_reason!r}."
        )

    base_score = FAILURE_REASON_BASE_SCORES[failure_reason]
    amount_bonus, value_category = _amount_contribution(payment.amount)
    score = min(base_score + amount_bonus, MAX_SCORE)
    priority = _priority_for_score(score)
    reason = (
        f"Failure reason {failure_reason} (base {base_score}) plus "
        f"{value_category} payment value (+{amount_bonus}) "
        f"yields score {score} ({priority})."
    )
    return RecoveryOpportunity(score=score, priority=priority, reason=reason)
