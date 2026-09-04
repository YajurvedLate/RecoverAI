import copy
import json
import os
import unittest
from unittest.mock import patch

from app.ai_agent import (
    ContextValidationError,
    DeterministicProvider,
    OpenAIProvider,
    OpenAIProviderError,
    RecommendationValidationError,
    RecoveryContext,
    RecoveryRecommendation,
    diagnose_with_fallback,
    payment_to_context,
    recommend_recovery,
    validate_context,
    validate_recommendation,
)
from app.models import Payment


PAISE_PER_RUPEE = 100


def _failed_payment(
    failure_reason: str,
    amount_rupees: int = 1000,
) -> Payment:
    """Helper to create a failed payment without database."""
    return Payment(
        id=1,
        merchant_id=1,
        customer_id="CUST-0001",
        amount=amount_rupees * PAISE_PER_RUPEE,
        currency="INR",
        status="FAILED",
        failure_reason=failure_reason,
    )


class ContextValidationTests(unittest.TestCase):
    """Test RecoveryContext validation."""

    def test_valid_context_passes(self) -> None:
        """Valid context passes validation."""
        context = RecoveryContext(
            payment_id=1,
            amount_paise=10000,
            currency="INR",
            payment_status="FAILED",
            failure_reason="BANK_TIMEOUT",
        )
        result = validate_context(context)
        self.assertEqual(result, context)

    def test_payment_id_zero_fails(self) -> None:
        """payment_id = 0 fails validation."""
        context = RecoveryContext(
            payment_id=0,
            amount_paise=10000,
            currency="INR",
            payment_status="FAILED",
            failure_reason="BANK_TIMEOUT",
        )
        with self.assertRaises(ContextValidationError):
            validate_context(context)

    def test_payment_id_negative_fails(self) -> None:
        """payment_id < 0 fails validation."""
        context = RecoveryContext(
            payment_id=-1,
            amount_paise=10000,
            currency="INR",
            payment_status="FAILED",
            failure_reason="BANK_TIMEOUT",
        )
        with self.assertRaises(ContextValidationError):
            validate_context(context)

    def test_amount_paise_negative_fails(self) -> None:
        """amount_paise < 0 fails validation."""
        context = RecoveryContext(
            payment_id=1,
            amount_paise=-1,
            currency="INR",
            payment_status="FAILED",
            failure_reason="BANK_TIMEOUT",
        )
        with self.assertRaises(ContextValidationError):
            validate_context(context)

    def test_amount_paise_zero_passes(self) -> None:
        """amount_paise = 0 passes validation."""
        context = RecoveryContext(
            payment_id=1,
            amount_paise=0,
            currency="INR",
            payment_status="FAILED",
            failure_reason="BANK_TIMEOUT",
        )
        result = validate_context(context)
        self.assertEqual(result.amount_paise, 0)

    def test_empty_currency_fails(self) -> None:
        """Empty currency fails validation."""
        context = RecoveryContext(
            payment_id=1,
            amount_paise=10000,
            currency="",
            payment_status="FAILED",
            failure_reason="BANK_TIMEOUT",
        )
        with self.assertRaises(ContextValidationError):
            validate_context(context)

    def test_whitespace_only_currency_fails(self) -> None:
        """Whitespace-only currency fails validation."""
        context = RecoveryContext(
            payment_id=1,
            amount_paise=10000,
            currency="   ",
            payment_status="FAILED",
            failure_reason="BANK_TIMEOUT",
        )
        with self.assertRaises(ContextValidationError):
            validate_context(context)

    def test_empty_payment_status_fails(self) -> None:
        """Empty payment_status fails validation."""
        context = RecoveryContext(
            payment_id=1,
            amount_paise=10000,
            currency="INR",
            payment_status="",
            failure_reason="BANK_TIMEOUT",
        )
        with self.assertRaises(ContextValidationError):
            validate_context(context)

    def test_whitespace_only_payment_status_fails(self) -> None:
        """Whitespace-only payment_status fails validation."""
        context = RecoveryContext(
            payment_id=1,
            amount_paise=10000,
            currency="INR",
            payment_status="   ",
            failure_reason="BANK_TIMEOUT",
        )
        with self.assertRaises(ContextValidationError):
            validate_context(context)

    def test_context_is_immutable(self) -> None:
        """RecoveryContext dataclass is frozen."""
        context = RecoveryContext(
            payment_id=1,
            amount_paise=10000,
            currency="INR",
            payment_status="FAILED",
            failure_reason="BANK_TIMEOUT",
        )

        with self.assertRaises(AttributeError):
            context.payment_id = 2

    def test_payment_to_context_validates(self) -> None:
        """payment_to_context validates the context it creates."""
        payment = _failed_payment("BANK_TIMEOUT", amount_rupees=100)
        context = payment_to_context(payment)

        # Verify context was validated and returned successfully
        self.assertEqual(context.payment_id, payment.id)
        self.assertEqual(context.amount_paise, payment.amount)
        self.assertEqual(context.currency, payment.currency)
        self.assertEqual(context.payment_status, payment.status)


class DiagnosisProviderTests(unittest.TestCase):
    """Test the deterministic diagnosis provider."""

    def setUp(self) -> None:
        self.provider = DeterministicProvider()

    def test_bank_timeout_recommends_retry(self) -> None:
        """BANK_TIMEOUT failure -> RETRY_PAYMENT."""
        payment = _failed_payment("BANK_TIMEOUT")
        context = payment_to_context(payment)
        recommendation = self.provider.diagnose(context)

        self.assertEqual(recommendation.recommended_action, "RETRY_PAYMENT")
        self.assertIn("timeout", recommendation.diagnosis.lower())
        self.assertGreater(len(recommendation.diagnosis), 0)
        self.assertGreater(len(recommendation.rationale), 0)
        self.assertEqual(recommendation.confidence, 0.95)

    def test_temporary_bank_error_recommends_retry(self) -> None:
        """TEMPORARY_BANK_ERROR failure -> RETRY_PAYMENT."""
        payment = _failed_payment("TEMPORARY_BANK_ERROR")
        context = payment_to_context(payment)
        recommendation = self.provider.diagnose(context)

        self.assertEqual(recommendation.recommended_action, "RETRY_PAYMENT")
        self.assertIn("Temporary", recommendation.diagnosis)
        self.assertEqual(recommendation.confidence, 0.95)

    def test_network_error_recommends_retry(self) -> None:
        """NETWORK_ERROR failure -> RETRY_PAYMENT."""
        payment = _failed_payment("NETWORK_ERROR")
        context = payment_to_context(payment)
        recommendation = self.provider.diagnose(context)

        self.assertEqual(recommendation.recommended_action, "RETRY_PAYMENT")
        self.assertIn("network", recommendation.diagnosis.lower())
        self.assertEqual(recommendation.confidence, 0.95)

    def test_bank_server_down_recommends_retry(self) -> None:
        """BANK_SERVER_DOWN failure -> RETRY_PAYMENT."""
        payment = _failed_payment("BANK_SERVER_DOWN")
        context = payment_to_context(payment)
        recommendation = self.provider.diagnose(context)

        self.assertEqual(recommendation.recommended_action, "RETRY_PAYMENT")
        self.assertIn("server", recommendation.diagnosis.lower())
        self.assertEqual(recommendation.confidence, 0.90)

    def test_card_expired_recommends_escalate(self) -> None:
        """CARD_EXPIRED failure -> ESCALATE."""
        payment = _failed_payment("CARD_EXPIRED")
        context = payment_to_context(payment)
        recommendation = self.provider.diagnose(context)

        self.assertEqual(recommendation.recommended_action, "ESCALATE")
        self.assertIn("expired", recommendation.diagnosis.lower())
        self.assertEqual(recommendation.confidence, 0.95)

    def test_card_declined_recommends_escalate(self) -> None:
        """CARD_DECLINED failure -> ESCALATE."""
        payment = _failed_payment("CARD_DECLINED")
        context = payment_to_context(payment)
        recommendation = self.provider.diagnose(context)

        self.assertEqual(recommendation.recommended_action, "ESCALATE")
        self.assertIn("declined", recommendation.diagnosis.lower())
        self.assertEqual(recommendation.confidence, 0.85)

    def test_insufficient_funds_recommends_escalate(self) -> None:
        """INSUFFICIENT_FUNDS failure -> ESCALATE."""
        payment = _failed_payment("INSUFFICIENT_FUNDS")
        context = payment_to_context(payment)
        recommendation = self.provider.diagnose(context)

        self.assertEqual(recommendation.recommended_action, "ESCALATE")
        self.assertIn("insufficient", recommendation.diagnosis.lower())
        self.assertEqual(recommendation.confidence, 0.90)

    def test_unknown_failure_reason_recommends_escalate(self) -> None:
        """Unknown failure reason -> ESCALATE."""
        payment = _failed_payment("UNKNOWN_REASON")
        context = payment_to_context(payment)
        recommendation = self.provider.diagnose(context)

        self.assertEqual(recommendation.recommended_action, "ESCALATE")
        self.assertIn("UNKNOWN_REASON", recommendation.diagnosis)
        self.assertEqual(recommendation.confidence, 0.8)

    def test_success_payment_recommends_escalate(self) -> None:
        """SUCCESS payment (not FAILED) -> ESCALATE."""
        payment = Payment(
            id=1,
            merchant_id=1,
            customer_id="CUST-0001",
            amount=1000 * PAISE_PER_RUPEE,
            currency="INR",
            status="SUCCESS",
            failure_reason=None,
        )
        context = payment_to_context(payment)
        recommendation = self.provider.diagnose(context)

        self.assertEqual(recommendation.recommended_action, "ESCALATE")
        self.assertIn("SUCCESS", recommendation.diagnosis)


class RecommendationValidationTests(unittest.TestCase):
    """Test RecoveryRecommendation validation."""

    def test_valid_recommendation_passes(self) -> None:
        """Valid recommendation passes validation."""
        rec = RecoveryRecommendation(
            diagnosis="Test diagnosis",
            recommended_action="RETRY_PAYMENT",
            confidence=0.9,
            rationale="Test rationale",
        )
        result = validate_recommendation(rec)
        self.assertEqual(result, rec)

    def test_empty_diagnosis_fails(self) -> None:
        """Empty diagnosis fails validation."""
        rec = RecoveryRecommendation(
            diagnosis="",
            recommended_action="RETRY_PAYMENT",
            confidence=0.9,
            rationale="Test rationale",
        )
        with self.assertRaises(RecommendationValidationError):
            validate_recommendation(rec)

    def test_whitespace_only_diagnosis_fails(self) -> None:
        """Whitespace-only diagnosis fails validation."""
        rec = RecoveryRecommendation(
            diagnosis="   ",
            recommended_action="RETRY_PAYMENT",
            confidence=0.9,
            rationale="Test rationale",
        )
        with self.assertRaises(RecommendationValidationError):
            validate_recommendation(rec)

    def test_empty_rationale_fails(self) -> None:
        """Empty rationale fails validation."""
        rec = RecoveryRecommendation(
            diagnosis="Test diagnosis",
            recommended_action="RETRY_PAYMENT",
            confidence=0.9,
            rationale="",
        )
        with self.assertRaises(RecommendationValidationError):
            validate_recommendation(rec)

    def test_whitespace_only_rationale_fails(self) -> None:
        """Whitespace-only rationale fails validation."""
        rec = RecoveryRecommendation(
            diagnosis="Test diagnosis",
            recommended_action="RETRY_PAYMENT",
            confidence=0.9,
            rationale="   ",
        )
        with self.assertRaises(RecommendationValidationError):
            validate_recommendation(rec)

    def test_unsupported_action_fails(self) -> None:
        """Unsupported action fails validation."""
        rec = RecoveryRecommendation(
            diagnosis="Test diagnosis",
            recommended_action="UNKNOWN_ACTION",
            confidence=0.9,
            rationale="Test rationale",
        )
        with self.assertRaises(RecommendationValidationError):
            validate_recommendation(rec)

    def test_confidence_below_zero_fails(self) -> None:
        """Confidence < 0.0 fails validation."""
        rec = RecoveryRecommendation(
            diagnosis="Test diagnosis",
            recommended_action="RETRY_PAYMENT",
            confidence=-0.1,
            rationale="Test rationale",
        )
        with self.assertRaises(RecommendationValidationError):
            validate_recommendation(rec)

    def test_confidence_above_one_fails(self) -> None:
        """Confidence > 1.0 fails validation."""
        rec = RecoveryRecommendation(
            diagnosis="Test diagnosis",
            recommended_action="RETRY_PAYMENT",
            confidence=1.1,
            rationale="Test rationale",
        )
        with self.assertRaises(RecommendationValidationError):
            validate_recommendation(rec)

    def test_confidence_zero_passes(self) -> None:
        """Confidence = 0.0 passes validation."""
        rec = RecoveryRecommendation(
            diagnosis="Test diagnosis",
            recommended_action="RETRY_PAYMENT",
            confidence=0.0,
            rationale="Test rationale",
        )
        result = validate_recommendation(rec)
        self.assertEqual(result.confidence, 0.0)

    def test_confidence_one_passes(self) -> None:
        """Confidence = 1.0 passes validation."""
        rec = RecoveryRecommendation(
            diagnosis="Test diagnosis",
            recommended_action="RETRY_PAYMENT",
            confidence=1.0,
            rationale="Test rationale",
        )
        result = validate_recommendation(rec)
        self.assertEqual(result.confidence, 1.0)

    def test_escalate_action_passes(self) -> None:
        """ESCALATE action is supported."""
        rec = RecoveryRecommendation(
            diagnosis="Test diagnosis",
            recommended_action="ESCALATE",
            confidence=0.5,
            rationale="Test rationale",
        )
        result = validate_recommendation(rec)
        self.assertEqual(result.recommended_action, "ESCALATE")

    def test_confidence_nan_fails(self) -> None:
        """Confidence = NaN fails validation."""
        rec = RecoveryRecommendation(
            diagnosis="Test diagnosis",
            recommended_action="RETRY_PAYMENT",
            confidence=float("nan"),
            rationale="Test rationale",
        )
        with self.assertRaises(RecommendationValidationError) as ctx:
            validate_recommendation(rec)
        self.assertIn("NaN", str(ctx.exception))

    def test_confidence_positive_infinity_fails(self) -> None:
        """Confidence = positive infinity fails validation."""
        rec = RecoveryRecommendation(
            diagnosis="Test diagnosis",
            recommended_action="RETRY_PAYMENT",
            confidence=float("inf"),
            rationale="Test rationale",
        )
        with self.assertRaises(RecommendationValidationError) as ctx:
            validate_recommendation(rec)
        self.assertIn("infinite", str(ctx.exception).lower())

    def test_confidence_negative_infinity_fails(self) -> None:
        """Confidence = negative infinity fails validation."""
        rec = RecoveryRecommendation(
            diagnosis="Test diagnosis",
            recommended_action="RETRY_PAYMENT",
            confidence=float("-inf"),
            rationale="Test rationale",
        )
        with self.assertRaises(RecommendationValidationError) as ctx:
            validate_recommendation(rec)
        self.assertIn("infinite", str(ctx.exception).lower())


class RecommendRecoveryTests(unittest.TestCase):
    """Test the recommend_recovery function."""

    def test_recommend_recovery_returns_validated_recommendation(self) -> None:
        """recommend_recovery returns a validated RecoveryRecommendation."""
        payment = _failed_payment("BANK_TIMEOUT")
        recommendation = recommend_recovery(payment)

        self.assertIsInstance(recommendation, RecoveryRecommendation)
        self.assertEqual(recommendation.recommended_action, "RETRY_PAYMENT")

    def test_recommend_recovery_does_not_modify_payment(self) -> None:
        """recommend_recovery does not modify the input payment."""
        payment = _failed_payment("BANK_TIMEOUT", amount_rupees=5000)
        original = copy.deepcopy(payment)

        recommend_recovery(payment)

        self.assertEqual(payment.status, original.status)
        self.assertEqual(payment.failure_reason, original.failure_reason)
        self.assertEqual(payment.amount, original.amount)
        self.assertEqual(payment.currency, original.currency)

    def test_recommend_recovery_is_deterministic(self) -> None:
        """Same payment always produces same recommendation."""
        payment = _failed_payment("CARD_EXPIRED")
        rec1 = recommend_recovery(payment)
        rec2 = recommend_recovery(payment)

        self.assertEqual(rec1.diagnosis, rec2.diagnosis)
        self.assertEqual(rec1.recommended_action, rec2.recommended_action)
        self.assertEqual(rec1.confidence, rec2.confidence)
        self.assertEqual(rec1.rationale, rec2.rationale)


class AIAgentSafetyTests(unittest.TestCase):
    """Test that AI agent respects safety constraints."""

    def test_ai_creates_no_database_records(self) -> None:
        """AI agent does not create any database records."""
        payment = _failed_payment("BANK_TIMEOUT")

        # recommend_recovery should complete without database access
        recommendation = recommend_recovery(payment)

        # Just verify it returned a recommendation
        self.assertIsInstance(recommendation, RecoveryRecommendation)

    def test_recommendation_is_immutable(self) -> None:
        """RecoveryRecommendation dataclass is frozen."""
        rec = RecoveryRecommendation(
            diagnosis="Test",
            recommended_action="RETRY_PAYMENT",
            confidence=0.9,
            rationale="Test",
        )

        with self.assertRaises(AttributeError):
            rec.diagnosis = "Modified"

    def test_ai_has_no_execution_behavior(self) -> None:
        """AI agent only returns recommendations, no side effects."""
        payment = _failed_payment("BANK_TIMEOUT")

        # This should just return data, no modifications
        recommendation = recommend_recovery(payment)

        # Verify it's just data
        self.assertIsInstance(recommendation, RecoveryRecommendation)
        # Payment is unchanged
        self.assertEqual(payment.status, "FAILED")


class MultipleFailureReasonTests(unittest.TestCase):
    """Test diagnosis for various failure reasons."""

    def test_all_retry_reasons_recommend_retry(self) -> None:
        """All temporary failure reasons recommend RETRY_PAYMENT."""
        retry_reasons = {
            "BANK_TIMEOUT",
            "TEMPORARY_BANK_ERROR",
            "NETWORK_ERROR",
            "BANK_SERVER_DOWN",
        }

        for reason in retry_reasons:
            with self.subTest(reason=reason):
                payment = _failed_payment(reason)
                rec = recommend_recovery(payment)
                self.assertEqual(rec.recommended_action, "RETRY_PAYMENT")

    def test_all_escalate_reasons_recommend_escalate(self) -> None:
        """All customer failure reasons recommend ESCALATE."""
        escalate_reasons = {
            "CARD_EXPIRED",
            "CARD_DECLINED",
            "INSUFFICIENT_FUNDS",
        }

        for reason in escalate_reasons:
            with self.subTest(reason=reason):
                payment = _failed_payment(reason)
                rec = recommend_recovery(payment)
                self.assertEqual(rec.recommended_action, "ESCALATE")


class ProviderAbstractionTests(unittest.TestCase):
    """Test that provider abstraction allows future LLM integration."""

    def test_deterministic_provider_is_replaceable(self) -> None:
        """DeterministicProvider implements abstract interface."""
        provider = DeterministicProvider()
        payment = _failed_payment("BANK_TIMEOUT")
        context = payment_to_context(payment)

        # Provider.diagnose should return a valid recommendation
        recommendation = provider.diagnose(context)
        self.assertIsInstance(recommendation, RecoveryRecommendation)

    def test_custom_provider_can_be_implemented(self) -> None:
        """Custom providers can implement the base interface."""
        from app.ai_agent import RecoveryContext, RecoveryDiagnosisProvider

        class CustomProvider(RecoveryDiagnosisProvider):
            def diagnose(self, context: RecoveryContext) -> RecoveryRecommendation:
                return RecoveryRecommendation(
                    diagnosis="Custom",
                    recommended_action="ESCALATE",
                    confidence=0.5,
                    rationale="Custom provider",
                )

        provider = CustomProvider()
        payment = _failed_payment("BANK_TIMEOUT")
        context = payment_to_context(payment)
        rec = provider.diagnose(context)

        self.assertEqual(rec.diagnosis, "Custom")
        self.assertEqual(rec.recommended_action, "ESCALATE")


class OpenAIProviderTests(unittest.TestCase):
    """Test OpenAI-backed diagnosis provider behavior."""

    def setUp(self) -> None:
        self.context = payment_to_context(_failed_payment("BANK_TIMEOUT"))

    def test_valid_structured_response_parses(self) -> None:
        """A valid structured JSON response is parsed into a recommendation."""

        class FakeResponse:
            output_parsed = {
                "diagnosis": "Bank timeout suggests temporary outage.",
                "recommended_action": "RETRY_PAYMENT",
                "confidence": 0.92,
                "rationale": "Temporary bank issue is usually transient.",
            }

        class FakeClient:
            def __init__(self):
                self.responses = self

            def create(self, **kwargs):
                return FakeResponse()

        provider = OpenAIProvider(api_key="test-key", client=FakeClient())
        recommendation = provider.diagnose(self.context)

        self.assertEqual(recommendation.recommended_action, "RETRY_PAYMENT")
        self.assertEqual(recommendation.confidence, 0.92)
        self.assertIn("Bank timeout", recommendation.diagnosis)

    def test_malformed_response_raises_provider_error(self) -> None:
        """Invalid JSON or missing required fields must fail safely."""

        class FakeResponse:
            output_parsed = {
                "diagnosis": "Missing action",
                "confidence": 0.95,
            }

        class FakeClient:
            def __init__(self):
                self.responses = self

            def create(self, **kwargs):
                return FakeResponse()

        provider = OpenAIProvider(api_key="test-key", client=FakeClient())
        with self.assertRaises(OpenAIProviderError):
            provider.diagnose(self.context)

    def test_missing_api_key_raises_provider_error(self) -> None:
        """Missing OPENAI_API_KEY must be rejected without making calls."""
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(OpenAIProviderError):
                OpenAIProvider()

    def test_api_failure_raises_provider_error(self) -> None:
        """Network or SDK errors are wrapped in a provider-level exception."""

        class FakeClient:
            def __init__(self):
                self.responses = self

            def create(self, **kwargs):
                raise RuntimeError("OpenAI API unavailable")

        provider = OpenAIProvider(api_key="test-key", client=FakeClient())
        with self.assertRaises(OpenAIProviderError):
            provider.diagnose(self.context)

    def test_deterministic_fallback_marks_fallback_in_rationale(self) -> None:
        """AI failure falls back to the deterministic provider and labels it."""

        class FailingClient:
            def __init__(self):
                self.responses = self

            def create(self, **kwargs):
                raise RuntimeError("boom")

        provider = OpenAIProvider(api_key="test-key", client=FailingClient())
        fallback = diagnose_with_fallback(self.context, provider=provider)

        self.assertEqual(fallback.recommended_action, "RETRY_PAYMENT")
        self.assertIn("deterministic fallback", fallback.diagnosis.lower())
        self.assertIn("deterministic fallback", fallback.rationale.lower())

    def test_provider_rejects_invalid_recommendation_payload(self) -> None:
        """Validation catches invalid action or confidence from the model."""

        class FakeResponse:
            output_parsed = {
                "diagnosis": "Invalid suggestion",
                "recommended_action": "UNKNOWN_ACTION",
                "confidence": 2.0,
                "rationale": "This should be rejected.",
            }

        class FakeClient:
            def __init__(self):
                self.responses = self

            def create(self, **kwargs):
                return FakeResponse()

        provider = OpenAIProvider(api_key="test-key", client=FakeClient())
        with self.assertRaises(OpenAIProviderError):
            provider.diagnose(self.context)


if __name__ == "__main__":
    unittest.main()
