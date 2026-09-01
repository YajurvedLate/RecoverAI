from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.database import Base, SessionLocal, engine
from app.models import Merchant, Payment

DEMO_MERCHANT_NAME = "RecoverAI Demo Merchant"
PAYMENT_COUNT = 180

# Temporary / infrastructure failures are more suitable for automatic retry.
# Card and funds failures are poorer candidates for unattended recovery.
FAILURE_REASONS = (
    "BANK_TIMEOUT",
    "TEMPORARY_BANK_ERROR",
    "NETWORK_ERROR",
    "INSUFFICIENT_FUNDS",
    "CARD_EXPIRED",
    "CARD_DECLINED",
)

# Amounts in paise (₹99 to ₹49,999).
LOW_VALUE_PAISE = (9900, 14900, 24900, 49900)
MID_VALUE_PAISE = (99900, 149900, 249900)
HIGH_VALUE_PAISE = (499900, 799900, 999900, 1999900, 4999900)

SEED_START = datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc)


def _amount_paise(index: int, *, failed: bool) -> int:
    if failed:
        bucket = index % 4
        if bucket == 0:
            return HIGH_VALUE_PAISE[index % len(HIGH_VALUE_PAISE)]
        if bucket == 1:
            return LOW_VALUE_PAISE[index % len(LOW_VALUE_PAISE)]
        return MID_VALUE_PAISE[index % len(MID_VALUE_PAISE)]

    bucket = index % 5
    if bucket == 0:
        return HIGH_VALUE_PAISE[index % len(HIGH_VALUE_PAISE)]
    if bucket in (1, 2):
        return LOW_VALUE_PAISE[index % len(LOW_VALUE_PAISE)]
    return MID_VALUE_PAISE[index % len(MID_VALUE_PAISE)]


def _build_payments(merchant_id: int) -> list[Payment]:
    payments: list[Payment] = []
    failed_index = 0

    for index in range(PAYMENT_COUNT):
        # 70 failed / 110 successful: 10 blocks of 18 with 7 failed each.
        failed = index % 18 < 7
        failure_reason = None
        if failed:
            failure_reason = FAILURE_REASONS[failed_index % len(FAILURE_REASONS)]
            failed_index += 1

        payments.append(
            Payment(
                merchant_id=merchant_id,
                customer_id=f"CUST-{index + 1:04d}",
                amount=_amount_paise(index, failed=failed),
                currency="INR",
                status="FAILED" if failed else "SUCCESS",
                failure_reason=failure_reason,
                created_at=SEED_START + timedelta(hours=index),
            )
        )

    return payments


def _format_inr(paise: int) -> str:
    rupees, remainder = divmod(paise, 100)
    return f"{rupees}.{remainder:02d}"


def _print_summary(
    *, merchant_label: str, total: int, successful: int, failed: int, failed_paise: int
) -> None:
    print(f"Merchant: {merchant_label}")
    print(f"Total payments: {total}")
    print(f"Successful payments: {successful}")
    print(f"Failed payments: {failed}")
    print(f"Failed revenue at risk: {_format_inr(failed_paise)} INR")


def main() -> None:
    Base.metadata.create_all(bind=engine)

    session = SessionLocal()
    try:
        merchant = session.scalar(
            select(Merchant).where(Merchant.name == DEMO_MERCHANT_NAME)
        )
        if merchant is None:
            merchant = Merchant(name=DEMO_MERCHANT_NAME, currency="INR")
            session.add(merchant)
            session.flush()
            merchant_label = f"created ({merchant.name})"
        else:
            merchant_label = f"found ({merchant.name})"

        session.execute(delete(Payment).where(Payment.merchant_id == merchant.id))
        session.add_all(_build_payments(merchant.id))
        session.commit()

        payments = session.scalars(
            select(Payment).where(Payment.merchant_id == merchant.id)
        ).all()
        successful = sum(1 for payment in payments if payment.status == "SUCCESS")
        failed_payments = [p for p in payments if p.status == "FAILED"]
        failed_paise = sum(payment.amount for payment in failed_payments)

        _print_summary(
            merchant_label=merchant_label,
            total=len(payments),
            successful=successful,
            failed=len(failed_payments),
            failed_paise=failed_paise,
        )
    finally:
        session.close()


if __name__ == "__main__":
    main()
