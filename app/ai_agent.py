import math
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.models import Payment


SUPPORTED_ACTIONS = {"RETRY_PAYMENT", "ESCALATE"}


@dataclass(frozen=True)
class RecoveryRecommendation:
    """
    AI recommendation for recovering a failed payment.
    
    Immutable dataclass that contains the AI's diagnosis and recommended action.
    This is structured output that will be validated before application use.
    """
    diagnosis: str
    recommended_action: str
    confidence: float
    rationale: str


class RecommendationValidationError(ValueError):
    """Raised when a RecoveryRecommendation fails validation."""
    pass


def validate_recommendation(recommendation: RecoveryRecommendation) -> RecoveryRecommendation:
    """
    Validate a RecoveryRecommendation before it is used by application logic.
    
    Checks:
    - Diagnosis is non-empty
    - Rationale is non-empty
    - Recommended action is supported (RETRY_PAYMENT or ESCALATE)
    - Confidence is a finite number between 0.0 and 1.0 inclusive
    
    Args:
        recommendation: The recommendation to validate.
    
    Returns:
        The same recommendation if valid.
    
    Raises:
        RecommendationValidationError: If validation fails.
    """
    if not recommendation.diagnosis or not recommendation.diagnosis.strip():
        raise RecommendationValidationError(
            "Diagnosis must be a non-empty string."
        )
    
    if not recommendation.rationale or not recommendation.rationale.strip():
        raise RecommendationValidationError(
            "Rationale must be a non-empty string."
        )
    
    if recommendation.recommended_action not in SUPPORTED_ACTIONS:
        raise RecommendationValidationError(
            f"Unsupported action {recommendation.recommended_action!r}. "
            f"Supported actions: {SUPPORTED_ACTIONS}."
        )
    
    # Check for NaN
    if math.isnan(recommendation.confidence):
        raise RecommendationValidationError(
            "Confidence must not be NaN."
        )
    
    # Check for infinity
    if math.isinf(recommendation.confidence):
        raise RecommendationValidationError(
            "Confidence must not be infinite."
        )
    
    if recommendation.confidence < 0.0 or recommendation.confidence > 1.0:
        raise RecommendationValidationError(
            f"Confidence must be between 0.0 and 1.0; got {recommendation.confidence}."
        )
    
    return recommendation


class RecoveryDiagnosisProvider(ABC):
    """
    Abstract base class for recovery diagnosis providers.
    
    This allows multiple implementations (deterministic local, LLM-based, etc.)
    to be plugged in without changing application code.
    """
    
    @abstractmethod
    def diagnose(self, payment: Payment) -> RecoveryRecommendation:
        """
        Diagnose a failed payment and recommend a recovery action.
        
        Args:
            payment: The payment to diagnose.
        
        Returns:
            A RecoveryRecommendation with diagnosis, action, and confidence.
        """
        pass


class DeterministicProvider(RecoveryDiagnosisProvider):
    """
    Deterministic diagnosis provider using failure taxonomy rules.
    
    Maps known failure reasons to recommendations based on RecoverAI's
    payment failure taxonomy. Unknown failures are escalated.
    
    This is not an LLM; it is a rule-based fallback for the MVP.
    """
    
    # Failure taxonomy: failure_reason -> (diagnosis, recommended_action)
    FAILURE_TAXONOMY = {
        "BANK_TIMEOUT": (
            "Temporary bank timeout",
            "RETRY_PAYMENT",
        ),
        "TEMPORARY_BANK_ERROR": (
            "Temporary bank error",
            "RETRY_PAYMENT",
        ),
        "NETWORK_ERROR": (
            "Network-related temporary failure",
            "RETRY_PAYMENT",
        ),
        "BANK_SERVER_DOWN": (
            "Bank server appears unavailable",
            "RETRY_PAYMENT",
        ),
        "CARD_EXPIRED": (
            "Customer card is expired",
            "ESCALATE",
        ),
        "CARD_DECLINED": (
            "Card payment was declined",
            "ESCALATE",
        ),
        "INSUFFICIENT_FUNDS": (
            "Customer has insufficient funds",
            "ESCALATE",
        ),
    }
    
    def diagnose(self, payment: Payment) -> RecoveryRecommendation:
        """
        Apply deterministic rules to diagnose the payment failure.
        
        Args:
            payment: The payment to diagnose.
        
        Returns:
            A RecoveryRecommendation based on the failure taxonomy.
        """
        # For non-FAILED payments, recommend escalation
        if payment.status != "FAILED":
            return RecoveryRecommendation(
                diagnosis=(
                    f"Payment status is {payment.status!r}, not FAILED. "
                    f"No automatic recovery is appropriate."
                ),
                recommended_action="ESCALATE",
                confidence=1.0,
                rationale=(
                    "Only FAILED payments require recovery. This payment "
                    "may require manual review or investigation."
                ),
            )
        
        failure_reason = payment.failure_reason
        
        # Check if failure reason is in the known taxonomy
        if failure_reason in self.FAILURE_TAXONOMY:
            diagnosis_text, action = self.FAILURE_TAXONOMY[failure_reason]
            return RecoveryRecommendation(
                diagnosis=diagnosis_text,
                recommended_action=action,
                confidence=1.0,
                rationale=(
                    f"Failure reason {failure_reason!r} is classified as "
                    f"{action.lower().replace('_', ' ')} in the failure taxonomy."
                ),
            )
        
        # Unknown failure reason: escalate
        return RecoveryRecommendation(
            diagnosis=f"Unknown failure reason: {failure_reason!r}",
            recommended_action="ESCALATE",
            confidence=0.8,
            rationale=(
                f"Failure reason {failure_reason!r} is not recognized in the "
                f"failure taxonomy. Manual review recommended."
            ),
        )


# Default provider instance
_default_provider = DeterministicProvider()


def recommend_recovery(payment: Payment) -> RecoveryRecommendation:
    """
    Diagnose a payment and recommend a recovery action.
    
    This function uses the default diagnosis provider (deterministic).
    The recommendation is validated before being returned.
    
    Important:
    - Does not modify the payment.
    - Does not access the database.
    - Does not call external services.
    - Returns only structured data for downstream processing.
    
    Args:
        payment: The payment to diagnose (not modified).
    
    Returns:
        A validated RecoveryRecommendation.
    """
    recommendation = _default_provider.diagnose(payment)
    return validate_recommendation(recommendation)
