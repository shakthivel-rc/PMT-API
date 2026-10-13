# Confirmation & exposure matrix

Generated from `03_capability_catalog.md`. Tier meanings: **T0** auto, **T1** auto + undo (user-configurable), **T2** confirm card, **T3** typed confirmation + cascade preview. "Catalog risk" is the label in the catalog; differences are explained in Notes.

Exposure: {'excluded': 25, 'enabled': 113, 'admin_only': 14, 'guided': 5} | Enabled by tier: {'T0': 46, 'T2': 36, 'T1': 29, 'T3': 2}

## auth

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `auth_login` | excluded | - | reversible | - | - | Authentication/credential flow. The chat session is already authenticated; never route credentials through an LLM. |
| `auth_logout` | excluded | - | reversible | - | - | Authentication/credential flow. The chat session is already authenticated; never route credentials through an LLM. Catalog marks this reversible; reclassified per tier rules. |
| `auth_refresh_token` | excluded | - | reversible | - | - | Authentication/credential flow. The chat session is already authenticated; never route credentials through an LLM. |
| `auth_register` | excluded | - | reversible | - | - | Authentication/credential flow. The chat session is already authenticated; never route credentials through an LLM. |
| `auth_verify_otp` | excluded | - | reversible | - | - | Authentication/credential flow. The chat session is already authenticated; never route credentials through an LLM. |
| `auth_resend_otp` | excluded | - | reversible | - | - | Authentication/credential flow. The chat session is already authenticated; never route credentials through an LLM. |
| `auth_set_password_registration` | excluded | - | reversible | - | - | Authentication/credential flow. The chat session is already authenticated; never route credentials through an LLM. |
| `auth_forgot_password` | excluded | - | reversible | - | - | Authentication/credential flow. The chat session is already authenticated; never route credentials through an LLM. |
| `auth_verify_forgot_password_token` | excluded | - | read-only | - | - | Authentication/credential flow. The chat session is already authenticated; never route credentials through an LLM. |
| `auth_reset_password` | excluded | - | reversible | - | - | Authentication/credential flow. The chat session is already authenticated; never route credentials through an LLM. |
| `auth_verify_account_token` | excluded | - | read-only | - | - | Authentication/credential flow. The chat session is already authenticated; never route credentials through an LLM. |
| `auth_set_password_invite` | excluded | - | reversible | - | - | Authentication/credential flow. The chat session is already authenticated; never route credentials through an LLM. |

## users

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `user_get_profile` | enabled | T0 | read-only | - | - |  |
| `user_change_password` | excluded | - | reversible | - | - | Password handling must never pass through an LLM context. Redirect to the profile screen. |
| `user_list` | excluded | - | read-only | - | - | Platform-wide directory callable by any authenticated user (cross-tenant leak). Use find_member (workspace_members_list) instead. |
| `user_get` | excluded | - | read-only | - | - | Platform-wide lookup callable by any authenticated user (cross-tenant leak). Use find_member instead. |
| `user_create` | admin_only | T1 | reversible | - | - | Platform user administration (manage_users). |
| `user_update` | admin_only | T2 | reversible | - | - | Platform user administration (manage_users). Account change. |
| `user_delete` | admin_only | T3 | destructive | - | - | Platform user administration (manage_users). |

## rbac

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `role_list` | admin_only | T0 | read-only | - | - | Platform RBAC. Disabled by default. |
| `role_get` | admin_only | T0 | read-only | - | - | Platform RBAC. Disabled by default. |
| `role_create` | admin_only | T1 | reversible | - | - | Platform RBAC. Disabled by default. |
| `role_update_permissions` | admin_only | T3 | reversible | - | - | Platform RBAC. Disabled by default. Changes permissions for every holder of the role. |
| `role_delete` | admin_only | T3 | destructive | - | - | Platform RBAC. Disabled by default. |
| `permission_list` | admin_only | T0 | read-only | - | - | Platform RBAC. Disabled by default. |
| `permission_get` | admin_only | T0 | read-only | - | - | Platform RBAC. Disabled by default. |
| `permission_create` | excluded | - | reversible | - | - | Platform permission catalog management. Admin console only. |
| `permission_update` | excluded | - | reversible | - | - | Platform permission catalog management. Admin console only. |
| `permission_delete` | excluded | - | destructive | - | - | Platform permission catalog management. Admin console only. |

## activity_logs

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `activity_log_list` | admin_only | T0 | read-only | - | - | Not workspace-scoped; holders of manage_users see every tenant's entries. Enable only after adding a workspace filter (see GAP-09). |

## health

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `health_live` | excluded | - | read-only | - | - | Operational health probe, not a user action. |
| `health_ready` | excluded | - | read-only | - | - | Operational health probe, not a user action. |
| `health_dependencies` | excluded | - | read-only | - | - | Operational health probe, not a user action. |

## workspaces

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `workspace_list` | enabled | T0 | read-only | - | - |  |
| `workspace_create` | enabled | T2 | destructive | - | - | Creates a new tenant. |
| `workspace_tree_get` | enabled | T0 | read-only | - | - |  |
| `workspace_update` | enabled | T2 | reversible | - | - | Renames the tenant. |
| `workspace_delete` | admin_only | T3 | destructive | workspace_tree_get, workspace_members_list | - | Cascade-deletes an entire tenant (all spaces, issues, time data). Disabled by default; enable explicitly per workspace policy. Also the most dangerous mis-resolution of "delete project X". |
| `workspace_members_list` | enabled | T0 | read-only | - | - |  |
| `workspace_member_add` | enabled | T2 | reversible | - | - | Grants workspace access. |
| `workspace_member_update_role` | enabled | T2 | reversible | - | - | Changes access level. |
| `workspace_member_remove` | enabled | T2 | reversible | workspace_members_list | - | Removes access. Catalog labels this reversible; re-adding does not restore per-user state. |

## hierarchy

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `space_get` | enabled | T0 | read-only | - | - |  |
| `space_create` | enabled | T2 | destructive | - | space_archive | Creates a top-level project container, workflow and key prefix. |
| `space_update` | enabled | T1 | reversible | - | - |  |
| `space_archive` | enabled | T2 | reversible | workspace_tree_get, sprint_list | space_unarchive |  |
| `space_unarchive` | enabled | T2 | reversible | - | space_archive |  |
| `space_freeze` | enabled | T2 | reversible | workspace_tree_get | space_unfreeze |  |
| `space_unfreeze` | enabled | T2 | reversible | - | space_freeze |  |
| `folder_create` | enabled | T1 | destructive | - | folder_archive | Catalog marks this destructive; creating is reversible by the matching delete/archive, so it is reclassified. |
| `folder_update` | enabled | T1 | reversible | - | - |  |
| `folder_archive` | enabled | T2 | reversible | workspace_tree_get | folder_unarchive |  |
| `folder_unarchive` | enabled | T2 | reversible | - | folder_archive |  |
| `list_create` | enabled | T1 | destructive | - | list_archive | Catalog marks this destructive; creating is reversible by the matching delete/archive, so it is reclassified. |
| `list_update` | enabled | T1 | reversible | - | - |  |
| `list_archive` | enabled | T2 | reversible | workspace_tree_get | list_unarchive |  |
| `list_unarchive` | enabled | T2 | reversible | - | list_archive |  |

## governance

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `governance_settings_get` | enabled | T0 | read-only | - | - |  |
| `governance_settings_update` | enabled | T2 | reversible | - | - | Changes WIP/transition/estimation rules for everyone. |

## issues

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `issue_list` | enabled | T0 | read-only | - | - |  |
| `issue_board_get` | enabled | T0 | read-only | - | - |  |
| `issue_backlog_get` | enabled | T0 | read-only | - | - |  |
| `issue_key_resolve` | enabled | T0 | read-only | - | - |  |
| `issue_get` | enabled | T0 | read-only | - | - |  |
| `issue_create` | enabled | T1 | destructive | - | issue_delete (no restore afterwards, GAP-05) | Catalog marks this destructive; creating is reversible by the matching delete/archive, so it is reclassified. |
| `issue_update` | enabled | T1 | reversible | issue_get | Re-apply previous values captured by pre-read (issue_get). |  |
| `issue_move` | enabled | T2 | reversible | issue_get, workspace_tree_get, workflow_get | issue_move back to the previous list (captured by pre-read). | Cross-space moves keep the old key prefix and may leave an invalid status (GAP-06; catalog and gap report disagree - verify in code). Warn on cross-space. |
| `issue_rank_backlog` | enabled | T1 | reversible | - | Re-rank to previous neighbours. |  |
| `issue_delete` | enabled | T3 | destructive | issue_get, comment_list, time_tracking_issue_get, dependency_list | - | Soft delete with NO restore endpoint (GAP-05) and detaches time entries. Downgrade to T2 once restore exists. |
| `comment_list` | enabled | T0 | read-only | - | - |  |
| `comment_create` | enabled | T1 | destructive | - | comment_delete | Catalog marks this destructive; creating is reversible by the matching delete/archive, so it is reclassified. |
| `comment_update` | enabled | T1 | reversible | comment_list | Re-apply previous content. |  |
| `comment_delete` | enabled | T2 | destructive | comment_list | - | Author or admin only. |
| `issue_ai_summary_request` | enabled | T1 | read-only | - | - | Catalog says read-only, but it enqueues a billable background AI job. |
| `issue_ai_job_get` | enabled | T0 | read-only | - | - |  |

## dependencies

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `dependency_list` | enabled | T0 | read-only | - | - |  |
| `dependency_create` | enabled | T1 | destructive | - | dependency_delete | Catalog marks this destructive; creating is reversible by the matching delete/archive, so it is reclassified. |
| `dependency_delete` | enabled | T1 | reversible | - | - | Link only. |

## sprints

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `sprint_list` | enabled | T0 | read-only | - | - |  |
| `sprint_create` | enabled | T1 | destructive | - | sprint_delete (planned only) | Catalog marks this destructive; creating is reversible by the matching delete/archive, so it is reclassified. |
| `sprint_get` | enabled | T0 | read-only | - | - |  |
| `sprint_update` | enabled | T1 | reversible | - | Re-apply previous values. |  |
| `sprint_delete` | enabled | T2 | destructive | sprint_get, sprint_issues_list | - | Planned sprints only; tickets return to the backlog (no data loss). |
| `sprint_start` | enabled | T2 | reversible | sprint_get, sprint_issues_list, governance_settings_get, sprint_list | - |  |
| `sprint_close` | enabled | T2 | reversible | sprint_get, sprint_issues_list, sprint_list | - | No reopen endpoint exists: treat as irreversible. Must ask where unfinished tickets go (backlog or a planned sprint). |
| `sprint_issues_list` | enabled | T0 | read-only | - | - |  |
| `sprint_issues_add` | enabled | T1 | reversible | sprint_get, issue_get | sprint_issues_remove |  |
| `sprint_issues_remove` | enabled | T1 | reversible | - | sprint_issues_add | Items return to backlog. |
| `sprint_report_get` | enabled | T0 | read-only | - | - |  |
| `sprint_burndown_get` | enabled | T0 | read-only | - | - |  |
| `space_velocity_get` | enabled | T0 | read-only | - | - |  |
| `space_cfd_get` | enabled | T0 | read-only | - | - |  |
| `space_cycle_time_get` | enabled | T0 | read-only | - | - |  |

## releases

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `release_list` | enabled | T0 | read-only | - | - |  |
| `release_create` | enabled | T1 | destructive | - | - | Catalog marks this destructive; creating is reversible by the matching delete/archive, so it is reclassified. |
| `release_get` | enabled | T0 | read-only | - | - |  |
| `release_update` | enabled | T1 | reversible | - | - |  |
| `release_delete` | enabled | T3 | destructive | release_get | - | Removes release and its fix-version links. |
| `release_ship` | enabled | T2 | reversible | release_get, release_notes_get | - | No un-ship endpoint exists: treat as irreversible. |
| `release_notes_get` | enabled | T0 | read-only | - | - |  |
| `release_fix_versions_list` | enabled | T0 | read-only | - | - |  |
| `release_fix_versions_set` | enabled | T1 | reversible | - | Re-apply previous release_ids. |  |

## saved_views

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `saved_view_list` | enabled | T0 | read-only | - | - |  |
| `saved_view_create` | enabled | T1 | destructive | - | saved_view_delete | Catalog marks this destructive; creating is reversible by the matching delete/archive, so it is reclassified. |
| `saved_view_get` | enabled | T0 | read-only | - | - |  |
| `saved_view_update` | enabled | T1 | reversible | - | - |  |
| `saved_view_delete` | enabled | T2 | destructive | - | - |  |

## time_tracking

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `time_tracking_issue_get` | enabled | T0 | read-only | - | - |  |
| `time_entry_create` | enabled | T1 | destructive | time_tracking_settings_get, timer_get | time_entry_delete | Catalog marks this destructive; creating is reversible by the matching delete/archive, so it is reclassified. |
| `time_entry_update` | enabled | T1 | reversible | time_entries_list | Re-apply previous values. |  |
| `time_entry_delete` | enabled | T2 | destructive | time_entries_list | - | Alters billing/payroll data. |
| `time_entries_list` | enabled | T0 | read-only | - | - |  |
| `time_report_get` | enabled | T0 | read-only | - | - |  |
| `timer_start` | enabled | T1 | reversible | - | timer_discard | One active timer per user globally. |
| `timer_get` | enabled | T0 | read-only | - | - |  |
| `timer_stop` | enabled | T1 | reversible | - | time_entry_delete | Creates a time entry. |
| `timer_discard` | enabled | T2 | reversible | - | - | Discards running time permanently. |
| `time_tracking_settings_get` | enabled | T0 | read-only | - | - |  |
| `time_tracking_settings_update` | enabled | T2 | reversible | - | - | Changes lock period / manual entry rules for everyone. |

## workflow

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `workflow_get` | enabled | T0 | read-only | - | - |  |
| `workflow_preview` | guided | T0 | read-only | - | - | Needs a full structured stage definition. Hand off to a form/editor card; the model should not free-type stage graphs. |
| `workflow_publish` | guided | T2 | reversible | - | - | Needs a full structured stage definition + stage_mappings and migrates every active ticket. Hand off to the workflow editor with a diff preview. |
| `workflow_templates_list` | enabled | T0 | read-only | - | - |  |
| `workflow_template_create` | enabled | T2 | destructive | - | - | Org-wide template. |
| `workflow_template_delete` | enabled | T2 | destructive | - | - |  |
| `workflow_editors_list` | enabled | T0 | read-only | - | - |  |
| `workflow_editors_set` | enabled | T2 | reversible | - | - | Changes who may edit workflows. |

## reports

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `report_catalog_get` | enabled | T0 | read-only | - | - |  |
| `report_run` | enabled | T0 | read-only | - | - |  |
| `report_export` | enabled | T1 | read-only | - | - |  |
| `report_jobs_list` | enabled | T0 | read-only | - | - |  |
| `report_job_get` | enabled | T0 | read-only | - | - |  |
| `report_job_download` | guided | T0 | read-only | - | - | Binary download. Return a signed link/card to the user, never stream content into the model. |
| `saved_reports_list` | enabled | T0 | read-only | - | - |  |
| `saved_report_create` | enabled | T1 | destructive | - | saved_report_delete | Catalog marks this destructive; creating is reversible by the matching delete/archive, so it is reclassified. |
| `saved_report_get` | enabled | T0 | read-only | - | - |  |
| `saved_report_update` | enabled | T1 | reversible | - | - |  |
| `saved_report_delete` | enabled | T2 | destructive | - | - |  |
| `report_schedules_list` | enabled | T0 | read-only | - | - |  |
| `report_schedule_create` | enabled | T2 | destructive | workspace_members_list | - | Recurring outbound email to external recipients (exfiltration vector). Recipients must be workspace members or on a domain allowlist. |
| `report_schedule_update` | enabled | T2 | reversible | workspace_members_list | - | Can redirect recurring outbound email. |
| `report_schedule_delete` | enabled | T2 | destructive | - | - |  |
| `report_schedule_run_now` | enabled | T2 | read-only | - | - | Catalog says read-only, but it sends email to the schedule recipients. |
| `reports_settings_get` | enabled | T0 | read-only | - | - |  |
| `reports_settings_update` | enabled | T2 | reversible | - | - | Changes export limits/retention for everyone. |

## storage

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `storage_settings_get` | admin_only | T0 | read-only | - | - | Workspace-admin infrastructure view. |
| `storage_settings_update` | excluded | - | reversible | - | - | Storage infrastructure/credentials. Admin console only. |
| `storage_settings_replace` | excluded | - | reversible | - | - | Storage infrastructure/credentials. Admin console only. |
| `storage_connection_test` | excluded | - | read-only | - | - | Storage infrastructure. Admin console only. |
| `issue_attachments_list` | enabled | T0 | read-only | - | - |  |
| `issue_attachment_upload` | guided | T1 | destructive | - | - | Binary upload. Use a chat file-upload bridge: the UI sends the file, the gateway passes a file reference, the model never sees bytes. Catalog marks this destructive; creating is reversible by the matching delete/archive, so it is reclassified. |
| `issue_attachment_download` | guided | T0 | read-only | - | - | Binary download. Return a signed link/card to the user, never stream content into the model. |
| `issue_attachment_delete` | enabled | T2 | destructive | issue_attachments_list | - |  |

## integrations

| Tool | Exposure | Tier | Catalog risk | Pre-reads | Undo | Notes |
|---|---|:---:|---|---|---|---|
| `github_repositories_list` | enabled | T0 | read-only | - | - |  |
| `github_repository_link` | enabled | T2 | destructive | - | - | Links an external system and stores a webhook secret; secret must be entered in the UI, not in chat. |
| `github_webhook_receive` | excluded | - | reversible | - | - | Inbound webhook endpoint (HMAC-signed by GitHub), not a user action. |
| `github_deferred_deliveries_list` | admin_only | T0 | read-only | - | - | Integration operations view. |

## Virtual (gateway) tools

| Tool | Kind | Tier | Built from | Closes |
|---|---|:---:|---|---|
| `ask_user` | interaction | T0 | - | - |
| `find_space` | resolver | T0 | workspace_tree_get | - |
| `find_list` | resolver | T0 | workspace_tree_get | - |
| `find_sprint` | resolver | T0 | sprint_list | - |
| `find_member` | resolver | T0 | workspace_members_list | - |
| `find_issue` | resolver | T0 | issue_key_resolve, issue_list | - |
| `knowledge_search` | rag | T0 | rag_index | - |
| `issue_clone` | composite | T2 | issue_get, issue_create, dependency_create | GAP-03 |
| `issue_bulk_update` | composite | T2 (<=25 items) / T3 (26-100); hard cap 100 | issue_update, issue_list, issue_delete | GAP-02 (until a native bulk endpoint exists) |
