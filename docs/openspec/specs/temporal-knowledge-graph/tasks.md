# Tasks: Temporal Knowledge Graph

Spec: SPEC-0001
Updated: 2026-09-18

## 1. Evidence-backed typed relations

- [x] 1.1 Store raw episodes and keep candidates untrusted until review (REQ
  "R1 External durable storage" and "R2 Evidence and trust layers", SPEC-0001).
- [x] 1.2 Extract explicit CODEOWNERS declarations into episode-linked pending
  typed candidates (REQ "R4 Evidence-backed typed relations", SPEC-0001).
- [x] 1.3 Add narrowly-scoped extractors for other explicit ownership and
  repository-mapping source formats, with evidence fixtures (REQ "R4
  Evidence-backed typed relations", SPEC-0001).

## 2. Validation and sweep selection

- [x] 2.1 Preserve deterministic validation outcomes and add direct CODEOWNERS
  ownership validation (REQ "R5 Deterministic validation and sweep history" and
  "R7 Error Handling Standards", SPEC-0001).
- [x] 2.2 Prioritise never-validated and unknown assertions with an explainable
  side-effect-free selection preview (REQ "R5 Deterministic validation and sweep
  history", SPEC-0001).
- [x] 2.3 Add predicate-specific validators for further authoritative source
  formats and fixtures for supported, contradicted, and unknown outcomes (REQ
  "R5 Deterministic validation and sweep history", SPEC-0001).
- [x] 2.4 Provide an external scheduled sweep runner with a bounded work budget
  and recorded run summaries (REQ "R3 Repository and temporal retrieval" and
  "R5 Deterministic validation and sweep history", SPEC-0001).

## 3. Recovery and evaluation

- [x] 3.1 Migrate legacy facts and candidates idempotently into structural
  episodes without inventing typed edges (REQ "R6 Migration and compatibility",
  SPEC-0001).
- [x] 3.2 Add interrupted-projection recovery, SQLite error-context, and
  assertion-derived SQL safety tests (REQ "R8 Database Operation Standards" and
  "R7 Error Handling Standards", SPEC-0001).
- [x] 3.3 Add a fixture corpus for repository/date isolation and graph-neighbour
  precision, then use it to guide queue consolidation (REQ "R3 Repository and
  temporal retrieval", SPEC-0001).
