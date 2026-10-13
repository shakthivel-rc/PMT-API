# Runbook: restructuring a workspace in waves

For the migration lead and the PMT administrator running a restructuring wave. It turns Phases
1–6 of `pmt-restructuring-plan.md` into steps against the tooling described in
[`docs/design/09-restructuring.md`](../design/09-restructuring.md) §7–§8. Every API path below is
under `/workspaces/{workspace_id}` and needs the **admin** role; the SPA's **Restructure** screen
(Projects › a workspace › Restructure) drives the same endpoints.

Stop and roll back (step 12) on any of: identity loss, history discrepancies, access errors,
broken integrations, invalid relationships.

## Phase 1 — Inventory and baseline

1. **Inventory.** `GET /inventory` (or *Restructure › Inventory*; *Export CSV* for the
   spreadsheet). Review every finding: sprint-named and status-named lists, duplicates, empty
   and inactive containers, spaces without an owner or charter, legacy relationships (task under
   story, bug under subtask), epics without owner or criteria, and **merge-before-acceptance**
   (a merge would mark work Done before QA). Resolve the merge finding first — plan §4's
   migration gate — by pointing the space's merge action at the acceptance stage.
2. **Baseline.** `POST /migration-baselines {"label": "before wave 1"}`. Keep the id. It records
   per-space totals and a per-issue fingerprint (key, location, stage, parent, comments, time,
   PR links, sprint rows, events).
3. **Backup, and prove it restores.** On the database host:

   ```bash
   # A consistent snapshot without locking writers (InnoDB).
   docker compose exec db sh -c 'exec mysqldump --single-transaction --routines --triggers \
       -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE"' > pmt-before-wave-1.sql
   # Restore into a throwaway database and compare row counts with production.
   docker compose exec -T db sh -c 'mysql -u root -p"$MYSQL_ROOT_PASSWORD" \
       -e "CREATE DATABASE pmt_restore_check"'
   docker compose exec -T db sh -c 'exec mysql -u root -p"$MYSQL_ROOT_PASSWORD" pmt_restore_check' \
       < pmt-before-wave-1.sql
   ```

   A backup that has not been restored is not a backup. Record where the file is kept.

## Phase 2 — Destination mappings (the register)

4. **Create destination containers first** with the ordinary screens (new space from the
   checklist, new lists or folders). Empty containers harm nothing and let the dry-run check
   against real targets.
5. **Create the plan.** `POST /migrations {"name": "...", "operations": [...]}`. Operation kinds:
   `move_issues`, `move_list_issues` (every issue of a list), `move_list` (folder change in the
   same space), `retype`, `reparent`, `set_status` (a status-named list → a stage), `archive`,
   `retain` (an explicit *leave it where it is* decision, with a reason). Cross-space moves need
   a stage mapping for every stage in play; canceled work can never be mapped to a completed
   stage.
6. **Dry-run.** `POST /migrations/{id}/validate`. Fix every problem it lists and validate again
   until it passes. Re-validation keeps approvals, owners and exceptions.
7. **Review the register.** `GET /migrations/{id}/register?format=csv`: one row per issue or
   container — old and new location, parent and stage mapping, sprint treatment, owner,
   exception, approval. Approve each row, set owners, record exceptions, or mark rows
   *retain*. Project leads and product owners sign off here.

## Phase 3–4 — Prerequisites and the pilot

8. **Rehearse on staging.** Restore the backup into staging and run steps 5–11 there with a
   representative team's data (parents and children, bugs, comments, time, PR links, closed
   sprints). Test planning, board moves, sprint close and carry-over, permissions, and the
   workflow editor (add, rename, reorder and remove stages; restricted flows with branching,
   skip and reopen; a non-default initial stage; a configured merge action). Get team and
   administrator sign-off before production.

## Phase 5 — Production, one wave at a time

9. **Pick the window.** Prefer a sprint boundary. Announce it.
10. **Approve and freeze.** `POST /migrations/{id}/approve` (captures a fresh baseline), then
    `POST /migrations/{id}/freeze`. Every affected space becomes read-only: people get a clear
    *"frozen while it is being reorganised"* message, running timers keep running, and GitHub
    deliveries for those spaces are **deferred**, not lost. Take a fresh backup (step 3) now.
11. **Execute.** `POST /migrations/{id}/validate` once more (the final dry-run), then
    `POST /migrations/{id}/execute` repeatedly until it reports nothing pending (the screen loops
    for you). Each batch is one transaction; a subtree never splits across batches; re-running
    never applies anything twice. If it stops as **failed**, read the item's error, fix the
    cause (or mark the item *retain*), and execute again — it resumes.
12. **Reconcile.** `GET /migrations/{id}/reconcile`. Every gate must pass:
    `mappings_complete`, `no_missing_or_duplicate`, `relationships_valid`,
    `history_reconciles`, `access_unchanged`, `keys_resolve`, `no_premature_done`,
    `workflow_consistent`. Spot-check old keys in GitHub and bookmarks: they must open the moved
    issues. If a gate fails and cannot be fixed forward while still frozen:
    `POST /migrations/{id}/rollback` (repeat until done). Rollback restores original keys and
    locations. Anything changed since it was migrated is **not** reverted — it is listed for
    forward repair, because reverting it would erase newer work.
13. **Reopen.** `POST /migrations/{id}/unfreeze`. PMT replays the deferred GitHub deliveries
    and reports how many; re-run reconcile afterwards and check a few replayed links.

After writes reopen, **do not restore the whole database** to undo a wave — it would erase work
people have done since. Use the rollback (which refuses drifted items) or forward repair.

## Phase 6 — Adoption and stabilisation

14. Publish [`docs/guides/structure-and-boards.md`](../guides/structure-and-boards.md) and train
    authorised users to customise workflows, preview space-wide impact and publish safely.
15. Daily support checks in the first week. Inspect two full sprint cycles (or an equivalent
    Kanban period) before decommissioning anything.
16. **Decommission** redundant containers only after reconciliation and sign-off: archive them
    (read-only, history kept) — never delete.
17. Review structure and exceptions monthly: re-run the inventory and compare with the baseline
    (`GET /migration-baselines/{id}/compare`).
