# Changelog

## 0.3.7

- Fix `BYSETPOS` candidate-range rules expanding into every listed date in Apple Reminders.
- Encode exact monthly dates directly: the 1st is `daysOfMonth=[1]`, the 21st is `daysOfMonth=[21]`, with no `setPositions`.
- Treat Things “last day of month” rules as manual/unsupported rather than creating a silently incorrect 31st-only or every-day schedule.
- Reject older plans containing non-empty `setPositions` during preflight/apply; regenerate them with v0.3.7.
- Bump the plan schema to `things-reminders-plan/v4` and add an end-to-end regression test for “every month on the 1st”.

## 0.3.6

> Superseded by v0.3.7: Reminders ignored `BYSETPOS` and expanded the candidate range.

- Fix Apple Reminders simplifying exact monthly dates such as the 21st into a generic Monthly rule.
- Encode each fixed month day as a non-simplifiable EventKit rule: candidate days `N...31` with `BYSETPOS=1`. For the 21st this is `BYMONTHDAY=21,22,...,31;BYSETPOS=1`.
- Split multiple exact month days into independent recurrence rules so each date remains exact.
- Keep the existing last-day representation (`28,29,30,31;BYSETPOS=-1`) unchanged.
- Verify both month-day candidates and set positions after EventKit saves the reminder.
- Add regression coverage for explicit and implicit 21st-day rules and multiple fixed month days.

## 0.3.5

> Superseded by v0.3.7: the `BYSETPOS` last-day workaround is not reliable for reminders.

- Fix Apple Reminders turning `BYMONTHDAY=-1` into “the 31st of every month”.
- Encode “last day of every month” as `BYMONTHDAY=28,29,30,31;BYSETPOS=-1`, selecting the final valid day in February and in 30-/31-day months.
- Preserve mixed rules such as “the 15th and the last day” as two EventKit recurrence rules.
- Add `setPositions` to plan schema v3 and verify it after every EventKit write.
- Keep plan v1/v2 decoding for existing runs and rollback compatibility.
- Add regression coverage for last-day-only and mixed fixed-day/last-day schedules.

## 0.3.4

- Materialize implicit monthly anchor rules as explicit EventKit `daysOfTheMonth` values.
- A Things item due on the 21st now produces a custom monthly rule with `BYMONTHDAY=21`, even when Things leaves its internal offset array empty.
- Preserve already-explicit month days and last-day-of-month (`-1`) rules unchanged.
- Add regression tests for implicit and explicit monthly dates.

## 0.3.3

- Exclude individually-open to-dos whose parent heading or project is completed, canceled, or trashed.
- Exclude structurally broken/missing parent references rather than importing them as Inbox tasks.
- Prevent recurring to-dos inside recurring projects from being imported as separate recurring reminders.
- List inactive-context exclusions in `report.txt`, `plan.json`, and `MANUAL_REVIEW.md`.
- Add regression tests for completed-project children, canceled-heading children, inactive recurring children, and nested recurrence inside recurring projects.

## 0.3.2

- Remove redundant Area and Repeat context from reminder notes.
- Stop embedding Things IDs, Things deep links, and migration IDs in new reminder notes.
- Render checklists and recurring-project blueprints with Markdown checkboxes.
- Move a note containing only one HTTP(S) URL into the native Reminders URL field.
- Use manifest-recorded EventKit IDs as the rollback identity for new runs.
- Keep verify/rollback compatibility with v0.3.1 plans and manifests.

## 0.3.1

- Fix monthly repeats on the last day of the month (`Things dy=-1` → `EventKit BYMONTHDAY=-1`).
- Keep unverified negative monthly offsets fail-closed.
- Add regression tests for both cases.

## 0.3.0

- Add safe recurring-project summary support.
