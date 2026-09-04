import json
import math
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.models import Payment

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - dependency is optional at import time
    OpenAI = None


SUPPORTED_ACTIONS = {"RETRY_PAYMENT", "ESCALATE"}


@dataclass(frozen=True)
class RecoveryContext:
    """
    Immutable context for AI diagnosis.

    This dataclass contains only the information the AI actually needs
    to diagnose a payment failure and recommend recovery action.

    Design principle: No ORM objects, no database session, no credentials.
    This is the contract boundary for AI providers.
    """
    payment_id: int
    amount_paise: int  # Integer paise (e.g., 1 rupee = 100 paise), never floats
    currency: str
    payment_status: str  # "FAILED", "SUCCESS", "PENDING"
    failure_reason: str | None
    risk_score: int | None = None  # For future LLM use
    priority: str | None = None  # For future LLM use


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


class ContextValidationError(ValueError):
    """Raised when a RecoveryContext fails validation."""
    pass


class OpenAIProviderError(RuntimeError):
    """Raised when the OpenAI diagnosis provider cannot produce a safe result."""
    pass


def validate_context(context: RecoveryContext) -> RecoveryContext:
    """
    Validate a RecoveryContext before it is used by providers.

    Checks:
    - payment_id must be positive (> 0)
    - amount_paise must be non-negative (>= 0)
    - currency must be non-empty
    - payment_status must be non-empty

    Note: This is structural validation only. Authorization decisions
    are made by the Policy Engine, not here.

    Args:
        context: The RecoveryContext to validate.

    Returns:
        The same context if valid.

    Raises:
        ContextValidationError: If validation fails.
    """
    if context.payment_id <= 0:
        raise ContextValidationError(
            f"payment_id must be positive; got {context.payment_id}."
        )

    if context.amount_paise < 0:
        raise ContextValidationError(
            f"amount_paise must be non-negative; got {context.amount_paise}."
        )

    if not context.currency or not context.currency.strip():
        raise ContextValidationError(
            "currency must be a non-empty string."
        )

    if not context.payment_status or not context.payment_status.strip():
        raise ContextValidationError(
            "payment_status must be a non-empty string."
        )

    return context


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

    Providers receive RecoveryContext (structured input with no ORM objects)
    and return RecoveryRecommendation (immutable output).

    No provider should:
    - Access the database directly
    - Modify any ORM objects
    - Make network calls without explicit rate limiting and error handling
    - Return unvalidated output
    """

    @abstractmethod
    def diagnose(self, context: RecoveryContext) -> RecoveryRecommendation:
        """
        Diagnose a payment failure and recommend a recovery action.

        Args:
            context: RecoveryContext with payment details (no ORM objects).

        Returns:
            A RecoveryRecommendation with diagnosis, action, and confidence.
        """
        pass


class OpenAIProvider(RecoveryDiagnosisProvider):
    """OpenAI-backed diagnosis provider using the Responses API.

    This provider is deliberately narrow: it only diagnoses and recommends,
    and it never executes payments or bypasses policy validation.
    """

    def __init__(
        self,
        api_key: str | None = None,
        client: Any | None = None,
        model: str = "gpt-4o-mini",
    ) -> None:
        if api_key is not None:
            resolved_key = api_key.strip()
        else:
            resolved_key = os.getenv("OPENAI_API_KEY", "").strip()

        if not resolved_key:
            raise OpenAIProviderError("OPENAI_API_KEY is not set.")

        self.api_key = resolved_key
        self.model = model
        self.client = client or self._build_client()

    def _build_client(self) -> Any:
        if OpenAI is None:
            raise OpenAIProviderError("OpenAI SDK is not installed.")
        return OpenAI(api_key=self.api_key)

    def _build_prompt(self, context: RecoveryContext) -> str:
        return (
            "You are analyzing a failed payment recovery case. "
            "Your role is diagnosis and recommendation only. "
            "You must not execute a payment, mutate any database records, "
            "call a recovery executor, or bypass the deterministic Policy Engine. "
            "The deterministic Policy Engine is the final authorization boundary. "
            "Never invent facts not present in RecoveryContext. "
            "The only valid recommendations are RETRY_PAYMENT or ESCALATE. "
            "Temporary technical failures such as BANK_TIMEOUT, NETWORK_ERROR, "
            "TEMPORARY_BANK_ERROR, and BANK_SERVER_DOWN are generally candidates for retry. "
            "Customer or payment-state failures such as CARD_EXPIRED, CARD_DECLINED, "
            "and INSUFFICIENT_FUNDS should generally be escalated rather than automatically retried. "
            "This is a recommendation only. "
            "Use the provided fields exactly and do not infer facts beyond them. "
            f"RecoveryContext fields:\n"
            f"payment_id={context.payment_id}\n"
            f"amount_paise={context.amount_paise}\n"
            f"currency={context.currency}\n"
            f"payment_status={context.payment_status}\n"
            f"failure_reason={context.failure_reason}\n"
            f"risk_score={context.risk_score}\n"
            f"priority={context.priority}\n"
        )

    def _response_format(self) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "name": "recovery_recommendation",
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "diagnosis": {"type": "string"},
                    "recommended_action": {
                        "type": "string",
                        "enum": ["RETRY_PAYMENT", "ESCALATE"],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "rationale": {"type": "string"},
                },
                "required": [
                    "diagnosis",
                    "recommended_action",
                    "confidence",
                    "rationale",
                ],
            },
            "strict": True,
        }

    def _extract_json(self, response: Any) -> dict[str, Any]:
        parsed = getattr(response, "output_parsed", None)
        if isinstance(parsed, dict):
            return parsed

        output = getattr(response, "output", None)
        if isinstance(output, list):
            for item in output:
                content = getattr(item, "content", None) or []
                for block in content:
                    text = getattr(block, "text", None)
                    if isinstance(text, str):
                        try:
                            payload = json.loads(text)
                            if isinstance(payload, dict):
                                return payload
                        except json.JSONDecodeError:
                            continue

        raise OpenAIProviderError("OpenAI returned an unparseable response payload.")

    def _coerce_recommendation(self, data: dict[str, Any]) -> RecoveryRecommendation:
        try:
            diagnosis = str(data["diagnosis"]).strip()
            action = str(data["recommended_action"]).strip()
            confidence = float(data["confidence"])
            rationale = str(data["rationale"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise OpenAIProviderError("OpenAI returned a recommendation missing required fields.") from exc

        recommendation = RecoveryRecommendation(
            diagnosis=diagnosis,
            recommended_action=action,
            confidence=confidence,
            rationale=rationale,
        )
        try:
            return validate_recommendation(recommendation)
        except RecommendationValidationError as exc:
            raise OpenAIProviderError(f"OpenAI returned an invalid recommendation: {exc}") from exc

    def diagnose(self, context: RecoveryContext) -> RecoveryRecommendation:
        validate_context(context)

        try:
            response = self.client.responses.create(
                model=self.model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": self._build_prompt(context),
                            }
                        ],
                    }
                ],
                text={"format": self._response_format()},
            )
        except Exception as exc:  # pragma: no cover - network conditions are exercised via fakes
            raise OpenAIProviderError("OpenAI diagnosis request failed.") from exc

        try:
            payload = self._extract_json(response)
            return self._coerce_recommendation(payload)
        except OpenAIProviderError:
            raise
        except Exception as exc:
            raise OpenAIProviderError("OpenAI response could not be transformed into a recommendation.") from exc


def diagnose_with_fallback(
    context: RecoveryContext,
    provider: RecoveryDiagnosisProvider | None = None,
    fallback_provider: RecoveryDiagnosisProvider | None = None,
) -> RecoveryRecommendation:
    """Attempt AI diagnosis and fall back gracefully to the deterministic provider.

    The deterministic fallback is explicitly labeled so downstream code never
    mistakes fallback output for model-generated advice.
    """
    if provider is None:
        try:
            provider = OpenAIProvider()
        except OpenAIProviderError as exc:
            fallback = fallback_provider or _default_provider
            deterministic = validate_recommendation(fallback.diagnose(context))
            return RecoveryRecommendation(
                diagnosis=f"Deterministic fallback: {deterministic.diagnosis}",
                recommended_action=deterministic.recommended_action,
                confidence=deterministic.confidence,
                rationale=(
                    "Deterministic fallback activated because the AI provider was unavailable: "
                    f"{exc}. {deterministic.rationale}"
                ),
            )

    try:
        return validate_recommendation(provider.diagnose(context))
    except Exception as exc:
        fallback = fallback_provider or _default_provider
        deterministic = validate_recommendation(fallback.diagnose(context))
        return RecoveryRecommendation(
            diagnosis=f"Deterministic fallback: {deterministic.diagnosis}",
            recommended_action=deterministic.recommended_action,
            confidence=deterministic.confidence,
            rationale=(
                "Deterministic fallback activated because the AI provider failed: "
                f"{exc}. {deterministic.rationale}"
            ),
        )


class DeterministicProvider(RecoveryDiagnosisProvider):
    """
    Deterministic diagnosis provider using failure taxonomy rules.

    Maps known failure reasons to recommendations based on RecoverAI's
    payment failure taxonomy. Unknown failures are escalated.

    This is not an LLM; it is a rule-based fallback for the MVP.
    It operates on RecoveryContext (not ORM objects).
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

    def diagnose(self, context: RecoveryContext) -> RecoveryRecommendation:
        """
        Apply deterministic rules to diagnose the payment failure.

        Args:
            context: RecoveryContext with payment details.

        Returns:
            A RecoveryRecommendation based on the failure taxonomy.
        """
        # For non-FAILED payments, recommend escalation
        if context.payment_status != "FAILED":
            return RecoveryRecommendation(
                diagnosis=(
                    f"Payment status is {context.payment_status!r}, not FAILED. "
                    f"No automatic recovery is appropriate."
                ),
                recommended_action="ESCALATE",
                confidence=1.0,
                rationale=(
                    "Only FAILED payments require recovery. This payment "
                    "may require manual review or investigation."
                ),
            )

        failure_reason = context.failure_reason
        reason_confidence_map = {
            "BANK_TIMEOUT": 0.95,
            "TEMPORARY_BANK_ERROR": 0.95,
            "NETWORK_ERROR": 0.95,
            "BANK_SERVER_DOWN": 0.90,
            "INSUFFICIENT_FUNDS": 0.90,
            "CARD_EXPIRED": 0.95,
            "CARD_DECLINED": 0.85,
        }

        # Check if failure reason is in the known taxonomy
        if failure_reason in self.FAILURE_TAXONOMY:
            diagnosis_text, action = self.FAILURE_TAXONOMY[failure_reason]
            confidence = reason_confidence_map.get(failure_reason, 0.8)
            return RecoveryRecommendation(
                diagnosis=diagnosis_text,
                recommended_action=action,
                confidence=confidence,
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


def payment_to_context(payment: Payment) -> RecoveryContext:
    """
    Extract RecoveryContext from a Payment object.

    This helper isolates ORM object usage to the entry point.
    Providers receive clean, structured RecoveryContext instead.
    The context is validated before being returned.

    Args:
        payment: The Payment ORM object (not modified).

    Returns:
        Validated RecoveryContext with only AI-relevant fields.

    Raises:
        ContextValidationError: If the extracted context is invalid.
    """
    context = RecoveryContext(
        payment_id=payment.id,
        amount_paise=payment.amount,
        currency=payment.currency,
        payment_status=payment.status,
        failure_reason=payment.failure_reason,
        risk_score=None,  # Could be populated if RecoveryCase is available
        priority=None,  # Could be populated if RecoveryCase is available
    )
    return validate_context(context)


def recommend_recovery(payment: Payment) -> RecoveryRecommendation:
    """
    Diagnose a payment and recommend a recovery action.

    This function:
    1. Extracts structured context from the Payment ORM object
    2. Passes context to the diagnosis provider
    3. Validates the recommendation before returning

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
    context = payment_to_context(payment)
    recommendation = _default_provider.diagnose(context)
    return validate_recommendation(recommendation)
