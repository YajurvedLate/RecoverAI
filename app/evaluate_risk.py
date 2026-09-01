from collections import defaultdict

from sqlalchemy import select

from app.database import SessionLocal
from app.models import Payment
from app.risk_engine import UnsupportedFailureReasonError, score_payment


def _format_inr(paise: int) -> str:
    rupees, remainder = divmod(paise, 100)
    return f"{rupees}.{remainder:02d}"


def main() -> None:
    session = SessionLocal()
    try:
        payments = session.scalars(
            select(Payment)
            .where(Payment.status == "FAILED")
            .order_by(Payment.id)
        ).all()

        priority_counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        priority_paise = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        reason_counts: dict[str, int] = defaultdict(int)
        reason_paise: dict[str, int] = defaultdict(int)
        total_failed_paise = 0
        unsupported = 0

        for payment in payments:
            total_failed_paise += payment.amount
            reason_key = payment.failure_reason or "MISSING"
            reason_counts[reason_key] += 1
            reason_paise[reason_key] += payment.amount

            try:
                result = score_payment(payment)
            except UnsupportedFailureReasonError as exc:
                unsupported += 1
                print(f"Unsupported failure reason for payment {payment.id}: {exc}")
                continue

            priority_counts[result.priority] += 1
            priority_paise[result.priority] += payment.amount

        print(f"Failed payments evaluated: {len(payments)}")
        print(f"HIGH count: {priority_counts['HIGH']}")
        print(f"MEDIUM count: {priority_counts['MEDIUM']}")
        print(f"LOW count: {priority_counts['LOW']}")
        if unsupported:
            print(f"Unsupported failure reasons: {unsupported}")
        print(f"HIGH revenue at risk: {_format_inr(priority_paise['HIGH'])} INR")
        print(f"MEDIUM revenue at risk: {_format_inr(priority_paise['MEDIUM'])} INR")
        print(f"LOW revenue at risk: {_format_inr(priority_paise['LOW'])} INR")
        print(f"Total failed revenue at risk: {_format_inr(total_failed_paise)} INR")
        print("Failure reason breakdown:")
        for reason in sorted(reason_counts):
            print(
                f"  {reason}: {reason_counts[reason]} payments, "
                f"{_format_inr(reason_paise[reason])} INR"
            )
    finally:
        session.close()


if __name__ == "__main__":
    main()
