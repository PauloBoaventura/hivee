---
name: hive.delegation-patterns
description: Concrete patterns for breaking colony work into parallel worker batches — when fan-out helps, how to decompose, batch sizing, and the tracker→skill→fan-out→validate loop.
metadata:
  author: hive
  type: default-skill
  visibility: [colony]
---

## Operational Protocol: Delegation Patterns

**Applies when** you're in COLONY mode and considering whether (and how) to fan out work to parallel workers via `run_parallel_workers`. Read this before fan-out, not during.

### The decision: should you fan out at all?

Fan-out helps when:
- The work has **N independent units** (rows, files, accounts, queries, segments) and each unit takes meaningful tool time (browser, API, file read, LLM call).
- The units are **disjoint** — no two workers need the same row, file, or external resource at the same time.
- You can describe one unit's work in <100 words once you've factored shared protocol into a skill. If a unit's spec is huge and unique, it's probably not parallel — it's just one task.

Fan-out HURTS when:
- N=1 or N=2 with cheap units. Spawning has overhead (fresh AgentLoop, separate conversation, no shared context). Below ~3 units of meaningful work, do it yourself.
- Units depend on each other's output (sequential pipeline). Parallel workers can't see each other's results mid-run; you'd be coordinating through the tracker, which is fine but adds round-trips.
- The work is exploratory ("figure out X"). Workers are bad at open-ended scope. Decompose first, then fan out the bounded parts.

When the user explicitly asks for fan-out, do not reject the request from an untested architecture guess. If you are unsure whether a browser session, API cursor, login, or other shared resource can be used by workers, run a one-worker probe first. The probe should only inspect read-only state (for example `browser_status`, `browser_tabs`, or a harmless page read) and report what it can access. Use that result to choose the full plan.

Browser and stateful-UI work has a safer pattern than "all workers click the same page." Prefer: parent extracts visible rows into the tracker, workers classify or enrich those rows in parallel, then the parent performs irreversible clicks/messages serially with verification. If the user explicitly accepts the risk of parallel UI control, workers must first discover their own browser state and claim work by stable visible identifiers, not by assumed scroll positions.

### The 4-step loop (always, in order)

  1. **Model the goal as a table** — `tracker_sql('CREATE TABLE …; INSERT …(seed keys you know);')`.
  2. **Write the protocol as a skill** — `write_skill(skill_name='<your-protocol>', skill_body='…')`.
  3. **Fan out** — `run_parallel_workers(tasks=[…unique slices…], skills=['<your-protocol>'])`.
  4. **Validate via SQL, not prose** — `tracker_sql('SELECT key FROM <table> WHERE <gap_condition>')`. Re-dispatch only the gap rows.

Every step is mandatory when the goal has row shape. Skipping step 1 means you read prose to find gaps. Skipping step 2 means you pay N× tokens for duplicated protocol. Skipping step 4 means you trust workers' summaries instead of verifying the actual data.

### Decomposition patterns

Pick ONE decomposition axis per fan-out. Mixing axes in the same batch usually means you should run two batches.

**Per-row** — One worker per row of the tracker table.
- Use when: each row is independently researchable / fillable (companies, papers, listings, tickets).
- Task string: `"Fill rows: <slug1>, <slug2>, ... Use <skill>."` plus the slug list.
- Batch size: 3–5 rows per worker. Below 3, parallelism overhead dominates; above 5, the worker's context bloats and one slow row blocks the rest.

**Per-segment** — One worker per slice of the data (alphabetical, geographic, time window).
- Use when: rows aren't seeded yet — workers DISCOVER them within their slice. E.g. "scrape competitors A–F", "scrape competitors G–M", "scrape competitors N–Z".
- Risk: workers might find the same row from different angles. Use the tracker's UNIQUE INDEX on the natural key to prevent duplicates (`INSERT OR IGNORE` in worker upserts).
- Batch size: as many segments as you can keep disjoint without overlap.

**Per-stage** — One worker per phase of a pipeline (gather, transform, validate).
- Use when: the work is sequential but each stage is itself parallel. NOT for two-stage chains where stage 2 needs all of stage 1's results — that's just two sequential `run_parallel_workers` calls.
- Use the tracker as the handoff: stage-1 workers fill columns A/B/C, stage-2 workers read those columns and fill D/E/F.

**Per-account / per-credential** — One worker per integrated identity.
- Use when: the work spans multiple authorized accounts (two LinkedIn profiles, two Slack workspaces, multiple GitHub orgs).
- Set `profile_name` on the task spec so each worker uses the right credentials. Check available aliases via `get_account_info()` if unsure.

### Batch sizing

- **No client-side cap.** Dispatch as many tasks as the goal needs in one `run_parallel_workers` call — the colony scheduler enforces the concurrency cap (`max_concurrent_workers`, set at incubation time via `create_colony(concurrency_hint=N)`, default 4). Tasks beyond the cap land in the runtime's pending queue and start automatically as running peers terminate. You'll see `running_now` and `queued` counts in the immediate tool return.
- **You don't need to manually split.** "Send 30 tasks" with cap=4 means 4 run, 26 queue, and the runtime promotes them in FIFO order as workers report.
- **Practical sweet spot per worker:** 2–10 minutes of work. Smaller and the spawn overhead dominates; larger and you hit the soft-timeout (default 600s).
- **If most of your batch ends up queued, consider raising the cap at incubation.** Setting `concurrency_hint=20` for a colony of light web fetches is reasonable; setting it for a colony driving a single browser is not (the workers will fight over the same browser instance).

### Skills for fan-out

Always write the protocol skill BEFORE the fan-out call. Skill body should contain:
1. The schema (which columns workers fill, what data type, what "complete" means).
2. The tool sequence (which tools to call in what order, e.g. "use `web_scrape` first, then `tracker_query` to check for an existing row, then `tracker_upsert`").
3. The output format (especially for any narrative columns: "2-3 sentences, no bullet points").
4. The quality bar (when to use 'N/A', when to flag uncertainty in a `confidence_notes` column).
5. Failure handling: "If you can't verify a field after 2 attempts, write 'N/A' with a one-line reason in `unverified_fields`. Don't fabricate."

Before writing the skill, restate the latest user constraints and put them in the protocol. If the user changes the task mid-run, the newer instruction wins over older criteria and over any previous skill draft.

The task string then carries ONLY the per-worker unique slice — typically the row keys to fill plus a one-line reminder to follow the skill.

### Anti-patterns (don't do these)

- **Duplicating shared context across task strings.** If you copy-paste the same paragraph across N task entries, stop and write a skill. Every duplicated word is billed N times.
- **Fan-out without a tracker.** Workers report prose; you read it to find gaps; you can't re-dispatch precisely. Always create the table first when the goal has row shape.
- **One mega-task disguised as N parallel ones.** If each task string is >500 words and most of it is unique to that worker, you don't have parallelizable work — you have N separate tasks that should each go through their own design pass.
- **Re-running the whole batch when 1 row failed.** Use `tracker_sql('SELECT key FROM t WHERE col IS NULL')` and dispatch ONLY the gap keys.
- **Treating worker failure as escalation.** Workers have no escalation channel — `report_to_parent(status='failed')` is terminal. On failure, you re-dispatch with different parameters (narrower scope, attached skill update, different model) or take over yourself. Don't try to "talk to" a failed worker.

### Worked example

Goal: "Research 25 fintech competitors and fill in funding, pricing, customer logos."

```
1. tracker_sql:
     CREATE TABLE competitors (
       slug TEXT PRIMARY KEY,
       name TEXT,
       website TEXT,
       funding_usd TEXT,
       pricing_model TEXT,
       customer_logos INTEGER,
       notes TEXT,
       researched_at TEXT
     );
     INSERT INTO competitors(slug, name) VALUES
       ('stripe','Stripe'), ('plaid','Plaid'), ... 25 rows ...;

2. tracker_register_writable(
     table='competitors',
     write_columns=['website','funding_usd','pricing_model',
                    'customer_logos','notes','researched_at'],
     key_columns=['slug'])

3. write_skill(
     skill_name='fintech-competitor-research',
     skill_body='# Protocol\\n\\nFor each assigned slug:\\n
       1. tracker_query("SELECT name FROM competitors WHERE slug=?") to confirm name.\\n
       2. web_scrape company website + crunchbase.\\n
       3. Fill these columns via tracker_upsert: website (URL), funding_usd
          ("$1.2B" / "$45M est." / "N/A"), pricing_model (one of: usage, seat,
          flat, hybrid), customer_logos (integer count from /customers page,
          or -1 if none found), notes (2-3 sentences VC-relevant), researched_at
          ("2026-05-08").\\n
       4. If a field can\\'t be verified after 2 attempts, write "N/A" and
          a one-line reason in notes. Don\\'t fabricate.')

4. run_parallel_workers(
     skills=['fintech-competitor-research'],
     tasks=[
       {task: 'Fill rows: stripe, plaid, ramp, brex, mercury'},
       {task: 'Fill rows: chime, robinhood, sofi, affirm, klarna'},
       {task: 'Fill rows: square, toast, rippling, gusto, deel'},
       {task: 'Fill rows: wise, revolut, n26, adyen, marqeta'},
       {task: 'Fill rows: alloy, persona, trulioo, jumio, socure'},
     ])

5. After [WORKER_REPORT]s arrive:
     tracker_sql("SELECT slug FROM competitors WHERE funding_usd IS NULL")
   If gap_keys is empty: done. Summarize the table for the user.
   Else: run_parallel_workers again with just those slugs in one task.
```

That's the whole pattern. Apply it to anything with row shape.
