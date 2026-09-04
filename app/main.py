import shutil
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.ai_agent import DeterministicProvider
from app.models import AuditEvent, Payment, RecoveryAction, RecoveryCase
from app.recovery_workflow import process_recovery_case, process_recovery_batch

app = FastAPI(title="RecoverAI Dashboard")


def format_inr(paise: int) -> str:
    rupees = paise / 100
    return f"₹{rupees:,.2f}"


def _copy_db_to_temp() -> tuple[Path, object]:
    source_db = Path(__file__).resolve().parent.parent / "recoverai.db"
    if not source_db.exists():
        raise FileNotFoundError(f"Missing seeded database: {source_db}")

    with NamedTemporaryFile(suffix=".db", delete=False) as tmp_file:
        tmp_path = Path(tmp_file.name)

    shutil.copy2(source_db, tmp_path)
    engine = create_engine(f"sqlite:///{tmp_path}", connect_args={"check_same_thread": False})
    return tmp_path, engine


def _get_demo_snapshot() -> dict:
    tmp_path, engine = _copy_db_to_temp()
    SessionLocalTmp = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    try:
        with SessionLocalTmp() as session:
            open_cases = session.scalars(
                select(RecoveryCase)
                .where(RecoveryCase.status == "OPEN")
                .order_by(RecoveryCase.id)
            ).all()

            revenue_at_risk_paise = 0
            for case in open_cases:
                payment = session.scalar(select(Payment).where(Payment.id == case.payment_id))
                if payment is not None:
                    revenue_at_risk_paise += payment.amount

            results = []
            for case in open_cases:
                results.append(
                    process_recovery_case(session, case, provider=DeterministicProvider())
                )

            recovered_revenue_paise = sum(
                result.recovered_amount_paise for result in results if result.execution_result == "RECOVERED"
            )
            recovered_count = sum(1 for result in results if result.execution_result == "RECOVERED")
            escalated_count = sum(1 for result in results if result.execution_result == "ESCALATED")
            blocked_count = sum(1 for result in results if result.execution_result == "BLOCKED")

            recovery_rate = 0
            if revenue_at_risk_paise:
                recovery_rate = int((recovered_revenue_paise * 100) // revenue_at_risk_paise)

            case_rows = []
            for result in results:
                case = session.get(RecoveryCase, result.case_id)
                payment = session.get(Payment, result.payment_id)
                if case is None or payment is None:
                    continue

                policy_decision = "AUTHORIZED" if result.policy_allowed else "BLOCKED"
                if result.recommended_action == "ESCALATE":
                    policy_decision = "N/A — ESCALATION"

                case_rows.append(
                    {
                        "case_id": result.case_id,
                        "payment_id": result.payment_id,
                        "amount_paise": payment.amount,
                        "amount_inr": format_inr(payment.amount),
                        "failure_reason": payment.failure_reason,
                        "risk_score": case.risk_score,
                        "priority": case.priority,
                        "recommendation": result.recommended_action,
                        "confidence": result.confidence,
                        "policy_decision": policy_decision,
                        "execution_result": result.execution_result,
                    }
                )

            return {
                "metrics": {
                    "revenue_at_risk_paise": revenue_at_risk_paise,
                    "revenue_recovered_paise": recovered_revenue_paise,
                    "recovery_rate": recovery_rate,
                    "failed_recovery_cases": len(open_cases),
                    "recovered_cases": recovered_count,
                    "escalated_cases": escalated_count,
                    "policy_blocked_cases": blocked_count,
                    "deterministic_fallback_cases": 0,
                    "revenue_at_risk_inr": format_inr(revenue_at_risk_paise),
                    "revenue_recovered_inr": format_inr(recovered_revenue_paise),
                    "provider_mode": "deterministic-demo",
                    "execution_mode_label": "Deterministic demo (no live OpenAI calls)",
                },
                "cases": case_rows,
            }
    finally:
        engine.dispose()
        if tmp_path.exists():
            tmp_path.unlink()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
def metrics() -> dict:
    snapshot = _get_demo_snapshot()
    return snapshot["metrics"]


@app.get("/cases")
def case_list() -> dict:
    snapshot = _get_demo_snapshot()
    return {"cases": snapshot["cases"]}


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    snapshot = _get_demo_snapshot()
    metrics = snapshot["metrics"]
    cases = snapshot["cases"]

    rows = "".join(
        f"<tr><td>{item['case_id']}</td><td>{item['payment_id']}</td><td>{item['amount_inr']}</td><td>{item['failure_reason']}</td><td>{item['risk_score']}</td><td>{item['priority']}</td><td>{item['recommendation']}</td><td>{item['confidence']}</td><td>{item['policy_decision']}</td><td>{item['execution_result']}</td></tr>"
        for item in cases
    )

    html = f"""
    <html>
      <head>
        <meta charset="utf-8" />
        <title>RecoverAI Dashboard</title>
        <style>
          body {{ font-family: Arial, sans-serif; margin: 24px; background: #f5f7fb; color: #1f2937; }}
          .header {{ margin-bottom: 20px; }}
          .alert {{ background: #eef2ff; color: #3730a3; padding: 12px 16px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #c7d2fe; }}
          .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin: 20px 0; }}
          .card {{ background: white; border: 1px solid #dfe7f5; border-radius: 10px; padding: 16px; box-shadow: 0 2px 6px rgba(15, 23, 42, 0.04); }}
          .label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; color: #6b7280; }}
          .value {{ font-size: 28px; font-weight: 700; margin-top: 8px; }}
          table {{ width: 100%; border-collapse: collapse; background: white; border: 1px solid #dfe7f5; border-radius: 10px; overflow: hidden; }}
          th, td {{ border-bottom: 1px solid #e5e7eb; padding: 10px 12px; text-align: left; font-size: 14px; }}
          th {{ background: #eef2ff; }}
          .badge {{ display: inline-block; padding: 4px 8px; border-radius: 999px; background: #dcfce7; color: #166534; font-size: 12px; font-weight: 600; }}
          .warning {{ color: #b45309; }}
        </style>
      </head>
      <body>
        <div class="header">
          <h1>RecoverAI Dashboard</h1>
          <div class="alert"><strong>AI recommends; deterministic policy authorizes.</strong></div>
          <div class="alert"><strong>Execution mode:</strong> {metrics['execution_mode_label']}</div>
        </div>

        <div class="cards">
          <div class="card"><div class="label">Revenue at risk</div><div class="value">{metrics['revenue_at_risk_inr']}</div></div>
          <div class="card"><div class="label">Revenue recovered</div><div class="value">{metrics['revenue_recovered_inr']}</div></div>
          <div class="card"><div class="label">Recovery rate</div><div class="value">{metrics['recovery_rate']}%</div></div>
          <div class="card"><div class="label">Failed / recovery cases</div><div class="value">{metrics['failed_recovery_cases']}</div></div>
          <div class="card"><div class="label">Recovered cases</div><div class="value">{metrics['recovered_cases']}</div></div>
          <div class="card"><div class="label">Escalated cases</div><div class="value">{metrics['escalated_cases']}</div></div>
          <div class="card"><div class="label">Policy-blocked cases</div><div class="value">{metrics['policy_blocked_cases']}</div></div>
        </div>

        <h2>Recovery cases</h2>
        <table>
          <thead>
            <tr>
              <th>Case ID</th>
              <th>Payment ID</th>
              <th>Amount</th>
              <th>Failure Reason</th>
              <th>Risk Score</th>
              <th>Priority</th>
              <th>Recommendation</th>
              <th>Diagnosis Confidence</th>
              <th>Policy Decision</th>
              <th>Execution Result</th>
            </tr>
          </thead>
          <tbody>
            {rows}
          </tbody>
        </table>
      </body>
    </html>
    """
    return html
