from sqlalchemy import select

from app.database import SessionLocal
from app.models import Payment, RecoveryCase
from app.risk_engine import UnsupportedFailureReasonError, score_payment


def main() -> None:
    session = SessionLocal()

    try:
        payments = session.scalars(
            select(Payment)
            .where(Payment.status == "FAILED")
            .order_by(Payment.id)
        ).all()

        created = 0
        existing = 0
        unsupported = 0

        priority_counts = {
            "HIGH": 0,
            "MEDIUM": 0,
            "LOW": 0,
        }

        cases_to_create: list[RecoveryCase] = []

        for payment in payments:
            existing_case = session.scalar(
                select(RecoveryCase).where(
                    RecoveryCase.payment_id == payment.id
                )
            )

            if existing_case is not None:
                existing += 1
                priority_counts[existing_case.priority] += 1
                continue

            try:
                result = score_payment(payment)
            except UnsupportedFailureReasonError as exc:
                unsupported += 1
                print(
                    f"Unsupported failure reason for payment "
                    f"{payment.id}: {exc}"
                )
                continue

            recovery_case = RecoveryCase(
                payment_id=payment.id,
                status="OPEN",
                risk_score=result.score,
                priority=result.priority,
            )

            cases_to_create.append(recovery_case)
            priority_counts[result.priority] += 1

        session.add_all(cases_to_create)
        session.commit()

        created = len(cases_to_create)

        print(f"Failed payments found: {len(payments)}")
        print(f"Recovery cases created: {created}")
        print(f"Recovery cases already existing/skipped: {existing}")
        print(f"Unsupported payments skipped: {unsupported}")
        print(f"HIGH cases: {priority_counts['HIGH']}")
        print(f"MEDIUM cases: {priority_counts['MEDIUM']}")
        print(f"LOW cases: {priority_counts['LOW']}")

    except Exception as exc:
        session.rollback()
        print(f"Recovery case creation failed; transaction rolled back: {exc}")
        raise

    finally:
        session.close()


if __name__ == "__main__":
    main()