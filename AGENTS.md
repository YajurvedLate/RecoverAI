# RecoverAI — AI Coding Instructions

## 1. Project Overview

RecoverAI is an AI-powered revenue recovery orchestration system for merchants.

The system identifies revenue at risk, gathers relevant context, diagnoses the likely cause, recommends an appropriate recovery intervention, validates the recommendation through deterministic policies, executes approved actions, measures outcomes, and maintains an audit trail.

The goal is to build a reliable, explainable, and measurable MVP for the Razorpay AI Buildathon.

---

## 2. Core Architecture

The project should maintain clear separation between:

- API layer
- Business logic
- Database/data access
- Risk engine
- AI agent
- Policy engine
- Action executor
- Audit logging
- Evaluation
- Frontend/dashboard

Prefer a modular monolith for the MVP.

Do not introduce microservices unless there is a clear and documented reason.

Keep business logic independent from framework-specific code where practical.

---

## 3. AI Safety Rules

The AI agent may diagnose cases and recommend actions.

The AI agent must never directly bypass the policy engine.

Raw LLM output must never directly trigger a financial action.

All executable recovery actions must pass deterministic policy validation.

Customer opt-out status, retry limits, action limits, case state, and other safety constraints must be enforced deterministically.

AI-generated structured output must be validated before being used by application logic.

Never expose API keys, credentials, tokens, or other secrets.

When uncertain, prefer a safe failure or escalation rather than an unsafe financial action.

---

## 4. Coding Principles

- Make small, focused changes.
- Do not modify unrelated files.
- Prefer simple solutions over unnecessary complexity.
- Do not introduce a dependency without a clear reason.
- Reuse existing project patterns.
- Keep business logic separate from AI-specific code.
- Keep functions and modules focused on a clear responsibility.
- Handle errors explicitly.
- Use clear names and readable code.
- Avoid hardcoding configuration or secrets.
- Do not invent requirements that were not requested.

---

## 5. Testing

After implementing a meaningful change:

1. Run relevant tests.
2. Fix failures before moving forward.
3. Add tests for important business logic.
4. Test both successful and failure paths.
5. Do not claim that a feature works without verifying it.

Important policy and recovery logic should have deterministic tests.

---

## 6. Git Rules

- Keep commits small and meaningful.
- Review changes before committing.
- Do not commit secrets.
- Do not commit `.env`.
- Do not commit `.venv`.
- Do not rewrite Git history unless explicitly requested.
- Use clear commit messages.
- Do not push unverified or obviously broken changes.

---

## 7. Agent Workflow

Before making a significant change:

1. Explain the proposed approach.
2. Identify the files that will change.
3. Implement the smallest reasonable change.
4. Run relevant tests.
5. Review the result for unintended changes.
6. Summarize what changed and why.

Do not build the entire application in one step.

When requirements are ambiguous, ask for clarification instead of inventing major requirements.

Prefer incremental, testable implementation.

When a problem is encountered, explain the likely cause before applying a fix when practical.

Document important architectural decisions and trade-offs.