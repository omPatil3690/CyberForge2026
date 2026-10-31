# Common Coding Agent Instructions

## 1. Purpose

This document defines the common instructions and development standards that every AI coding agent must follow while working on this project.

These instructions are tool-independent and apply to all coding agents, including Claude Code, OpenAI Codex, Cursor, GitHub Copilot, and any other AI-assisted development tool.

**Every agent must read and understand this document before making any modifications to the codebase.**

The primary objective is to maintain consistency, transparency, traceability, and proper documentation across all development changes, regardless of which agent performs the work.

---

## 2. Mandatory Changelog Maintenance

Every codebase modification MUST be documented in the project's changelog.

This is a mandatory requirement, not an optional documentation practice.

### 2.1 When to Update the Changelog

An agent must add a changelog entry whenever it:

- Adds a new feature or functionality.
- Modifies existing functionality or business logic.
- Fixes a bug or resolves an issue.
- Adds, modifies, or removes an API endpoint.
- Changes database schemas, tables, relationships, migrations, or queries.
- Modifies application architecture or system design.
- Adds, removes, or updates dependencies or packages.
- Changes configuration, environment handling, or deployment settings.
- Modifies frontend components, UI, styling, or user interactions.
- Updates authentication, authorization, security, or validation logic.
- Refactors code in a way that changes its structure or implementation.
- Removes obsolete code, files, modules, or functionality.
- Modifies tests or testing infrastructure.

Even if a modification is small, it must be recorded.

Purely investigative activities, such as reading files or explaining code without modifying anything, do not require a changelog entry.

### 2.2 Changelog Location

Maintain a single centralized changelog file:

`CHANGELOG.md`

If the project already has an established changelog location, continue using it rather than creating a duplicate.

### 2.3 Changelog Entry Format

Every entry must follow this exact structure:

**Change Number:** Sequential unique number.

**Timestamp:** Date and time of the change, including timezone.

**Agent/Author:** Name of the coding agent or developer responsible for the modification.

**Change Title:** Short, descriptive title.

**Details:** Bullet points explaining exactly what was added, modified, fixed, or removed.

**Affected Files:** List of files modified or created.

**Change Type:** Feature / Enhancement / Bug Fix / Refactoring / Configuration / Database / Documentation / Testing / Removal.

Use the following format:

#### Change #001 — [Change Title]

- **Timestamp:** YYYY-MM-DD HH:MM:SS IST (UTC+05:30)
- **Agent/Author:** [Agent Name]
- **Type:** [Change Type]
- **Description:**
  - Explain what was added or changed.
  - Explain how the implementation works.
  - Mention relevant technical details.
  - Explain the impact of the change.
- **Affected Files:**
  - `path/to/file1`
  - `path/to/file2`

---

## 3. Change Numbering Convention

1. Every changelog entry must have a unique, sequential change number.
2. Always inspect the existing changelog before creating a new entry.
3. Identify the latest recorded change number.
4. Increment the number by one for the next change.
5. Never reuse an existing change number.
6. Never reset numbering, even when a new coding agent starts working on the project.
7. If multiple changes are made in one development session, create separate entries for logically distinct changes.
8. If multiple agents are working simultaneously, inspect the latest changelog before committing changes and resolve numbering conflicts.

Example:

- Change #001 — Initial Project Setup
- Change #002 — Implemented User Authentication
- Change #003 — Added Redis Caching
- Change #004 — Fixed Database Connection Handling
- Change #005 — Improved API Error Handling

---

## 4. Timestamp Convention

- Always record the actual date and time when the modification is completed.
- Use the `YYYY-MM-DD HH:MM:SS` format.
- Include the timezone explicitly.
- Use IST (UTC+05:30) unless the project specifies another timezone.
- Never invent or estimate timestamps.
- If the exact time is unavailable, use the actual available system timestamp at the time of recording.

Example:

`2026-10-31 14:35:22 IST (UTC+05:30)`

---

## 5. Agent Identification

Every coding agent must identify itself in the changelog.

Examples:

- OpenAI Codex
- Claude Code
- Cursor Agent
- GitHub Copilot
- Developer — [Name]
- Other AI Agent — [Tool/Model Name]

If the exact model name is unknown, record the available tool or agent name instead of guessing.

---

## 6. Description Quality Standards

Changelog descriptions must be meaningful, specific, and understandable to another developer.

**Avoid vague descriptions such as:**

- Updated some files.
- Fixed the code.
- Improved the backend.
- Made changes to the UI.
- Optimized the application.

**Use specific descriptions such as:**

- Implemented Redis-based caching for frequently accessed user records to reduce repeated PostgreSQL queries.
- Added JWT-based authentication middleware to validate access tokens before protected API requests.
- Introduced database indexes on frequently queried columns to improve query performance.
- Fixed an issue where expired refresh tokens were not properly rejected during authentication.
- Added pagination and filtering support to the user retrieval API.

A developer who has never worked on the project should be able to understand the purpose and impact of each change by reading the changelog.

---

## 7. Mandatory Development Workflow

Every coding agent must follow this workflow before and after making modifications.

### Before Making Changes

1. Read this `instructions.md` file completely.
2. Understand the project's existing structure and architecture.
3. Inspect the existing `CHANGELOG.md`.
4. Identify the latest change number.
5. Understand the relevant implementation before modifying it.
6. Avoid unnecessarily duplicating existing functionality.
7. Identify the files and components that will be affected.

### While Making Changes

1. Follow the existing coding conventions and architectural patterns.
2. Make focused changes rather than modifying unrelated files.
3. Preserve existing functionality unless explicitly instructed otherwise.
4. Avoid unnecessary refactoring.
5. Do not introduce additional dependencies without justification.
6. Handle errors, edge cases, and validations appropriately.
7. Follow existing security and configuration practices.

### After Making Changes

1. Verify the implementation.
2. Run relevant tests, builds, or validation commands where possible.
3. Identify all files created, modified, or removed.
4. Update `CHANGELOG.md` with the next sequential change number.
5. Include the actual timestamp and agent name.
6. Explain the modification using meaningful bullet points.
7. Verify that the changelog entry accurately reflects the implemented changes.
8. Include both code modifications and the changelog update in the final change set.

**Important:** Never consider a development task complete until its corresponding changelog entry has been added and verified.

---

## 8. Handling Multiple Changes in One Task

When a task involves multiple logically distinct modifications, document them separately.

For example, if a task involves:

- Adding a new API endpoint.
- Creating a database migration.
- Updating the frontend to consume the API.

These should be documented as separate changes if they represent independently understandable modifications.

However, do not create excessive entries for individual lines of code or trivial modifications within the same logical feature.

Use engineering judgment to maintain a useful level of granularity.

---

## 9. Handling Failed or Reverted Changes

- Do not document an implementation as successfully completed if it failed.
- If a previously documented change is reverted, create a new changelog entry describing the reversal.
- Never silently delete historical changelog entries.
- If an implementation is partially completed, clearly document its current status and limitations.
- Record follow-up fixes as new changes rather than overwriting historical records.

---

## 10. Final Response Format

After completing any development task, every agent must provide a concise summary containing:

**1. Changes Implemented**

- Explain the functionality added or modified.

**2. Files Modified**

- List the relevant files.

**3. Validation**

- Mention tests, builds, or checks performed.
- Clearly state anything that was not tested.

**4. Changelog Updated**

- Mention the change number.
- Mention the changelog title.
- Confirm that the timestamp and agent details were recorded.

Example:

Task completed successfully.

- Implemented Redis caching for user profile retrieval.
- Added cache invalidation when user profile data is updated.
- Modified `user_service.py` and `cache.py`.
- Verified using the relevant backend tests.

**Changelog:** Updated `CHANGELOG.md` with Change #012 — Redis Caching for User Profiles.

---

## 11. Non-Negotiable Rules

1. Never modify project files without first understanding the existing implementation.
2. Never skip changelog maintenance.
3. Never reuse or reset change numbers.
4. Never fabricate timestamps, test results, or implementation details.
5. Never record changes that were not actually implemented.
6. Never remove historical changelog entries.
7. Never mark a task as complete while its required changelog entry is missing.
8. Always maintain backward compatibility unless explicitly instructed otherwise.
9. Always prioritize code quality, maintainability, and consistency with the existing architecture.
10. These instructions apply to every coding agent and every development session.

---

**Final Principle:**

Every modification to this codebase must be traceable, understandable, and attributable.

Regardless of which coding agent performs the work, the project's development history must remain consistent through a single, sequential, timestamped, and descriptive changelog.
