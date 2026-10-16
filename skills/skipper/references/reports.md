# Compact reports

Send one short transition report; omit unchanged fields and static enrollment
metadata. Keep task/command IDs stable. Use this line format; add evidence only
when it changes:

```text
REPORT TASK_ID STATE | evidence=REF | blocked=REASON_OR_NONE | next=ACTION
FYI/no-reply TASK_ID | evidence=REF | change=FACT
QUESTION QUESTION_ID | question=TEXT | options=OPTIONS | recommendation=TEXT | buddy=POSITION | urgency=LEVEL
```

Use `accepted`, `blocked`, `ready`, `incident` or `done` for transition STATE;
state the actual outcome and relevant SHA. Include buddy name/UUID and grant
references at enrollment or identity changes, not in every routine report.
For questions, state `buddy agrees`, `buddy dissents: ...` or `buddy not consulted`.
Never call a recommendation the pair's without that position. Batch independent
FYIs into one message; send urgent incidents and user blockers immediately.

Use `FYI/no-reply` only for information with no requested action, decision or
acknowledgment. Process it and record material changes without a courtesy reply
or echo to the user; report through the normal change-only update. If it reveals
an incident, conflict or owed action, send the required transition/escalation.
This is a content convention, not a transport flag or a delivery guarantee;
it still wakes a recipient, and a shim may emit a completion notice. Preserve
any runtime-required reply path and correlated response protocol.

Never mark a grant, enrollment, command acceptance, question answer, review,
handover, stop, hold acknowledgment or release as no-reply. Keep their exact
identity, evidence, verdict and acknowledgment requirements. A compact report
records only the gates actually satisfied; queued transport proves no acceptance.
Use `record --batch` for already-received independent ledger updates, retaining
one validated event per update. Never fabricate an acknowledgment to fill a batch.
