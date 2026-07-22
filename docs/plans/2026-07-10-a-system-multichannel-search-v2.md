# A System Multi-Channel Search v2 Implementation Plan

> **For Hermes:** Execute this plan task-by-task with strict RED-GREEN-REFACTOR verification.

**Goal:** Upgrade `multi-channel-search` from a company-report workflow into a job-driven A System workflow while preserving its legacy company talent-map mode.

**Architecture:** Add a standard-library Python orchestrator inside the skill. It reads the v3 position library, builds an exclusion set and channel-specific query plan, validates channel readiness, normalizes staged candidates, and emits dry-run or guarded intake operations. The skill becomes a router between `a-system-job` and `company-map` modes.

**Tech Stack:** Python 3, SQLite, unittest, Chrome CDP HTTP endpoints, A System v3 database and sync scripts.

---

### Task 1: Define Position Context Contract

**Objective:** Resolve one canonical open job and its position profile from the v3 database.

**Files:**
- Create: `/Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py`
- Create: `/Users/messi/.codex/skills/multi-channel-search/tests/test_a_system_multichannel.py`

**Steps:**
1. Write tests for exact job resolution, ambiguous/missing jobs, and source position ID validation.
2. Run the focused test and confirm failure because the module does not exist.
3. Implement `load_position_context()` with read-only SQLite access.
4. Run focused and full tests; expect pass.

### Task 2: Build Historical Exclusion Set

**Objective:** Exclude existing, contacted, stopped, rejected, and duplicate candidates before channel review.

**Files:**
- Modify: `/Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py`
- Modify: `/Users/messi/.codex/skills/multi-channel-search/tests/test_a_system_multichannel.py`

**Steps:**
1. Write tests covering local candidate IDs, person fingerprints, masked-name/company/title keys, and manual-stop precedence.
2. Run tests and confirm the new assertions fail.
3. Implement `load_exclusion_set()` and `classify_duplicate()`.
4. Run tests; expect pass.

### Task 3: Generate Channel-Specific Search Plan

**Objective:** Produce job-profile queries for Liepin and X-SaaS without hard-coded role families.

**Files:**
- Modify: `/Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py`
- Modify: `/Users/messi/.codex/skills/multi-channel-search/tests/test_a_system_multichannel.py`

**Steps:**
1. Write tests for search keyword reuse, target-company expansion, stop-reason-derived negative rules, and deterministic query limits.
2. Confirm test failure.
3. Implement `build_search_plan()` returning structured rounds and review gates.
4. Run tests; expect pass.

### Task 4: Add Channel Readiness Gates

**Objective:** Prevent login pages, generic recommendation feeds, stale X-SaaS caches, and unsubmitted Liepin queries from being recorded as valid searches.

**Files:**
- Modify: `/Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py`
- Modify: `/Users/messi/.codex/skills/multi-channel-search/tests/test_a_system_multichannel.py`

**Steps:**
1. Write tests for ready, login-required, invalid-search, and unavailable CDP states.
2. Confirm failure.
3. Implement pure response classifiers plus CDP preflight collection.
4. Run tests; expect pass.

### Task 5: Normalize and Stage Candidates

**Objective:** Normalize channel records and generate safe A System intake operations without direct outreach.

**Files:**
- Modify: `/Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py`
- Modify: `/Users/messi/.codex/skills/multi-channel-search/tests/test_a_system_multichannel.py`

**Steps:**
1. Write tests for company/title extraction, school-fragment rejection, cross-channel duplicate detection, and `S1`/`X1` staging.
2. Confirm failure.
3. Implement normalization and a transaction-backed `apply_intake()` guarded by `--apply`.
4. Ensure source candidate IDs use local v3 candidate IDs and events retain source evidence.
5. Run tests; expect pass.

### Task 6: Add CLI and Dry-Run Receipt

**Objective:** Expose auditable `context`, `plan`, `preflight`, and `intake` commands.

**Files:**
- Modify: `/Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py`
- Modify: `/Users/messi/.codex/skills/multi-channel-search/tests/test_a_system_multichannel.py`

**Steps:**
1. Write CLI tests for JSON output, non-zero validation failures, and no database mutation in dry-run mode.
2. Confirm failure.
3. Implement argparse commands and receipt generation.
4. Run tests; expect pass.

### Task 7: Upgrade Skill Routing

**Objective:** Make A System job mode the default when a canonical v3 job exists and retain company-map mode only for explicit company mapping.

**Files:**
- Modify: `/Users/messi/.codex/skills/multi-channel-search/SKILL.md`

**Steps:**
1. Replace the old single-mode workflow with explicit mode detection.
2. Document A System data contracts, review stages, outreach boundaries, sync, and verification.
3. Keep legacy HTML/company-map instructions in a clearly scoped section.
4. Validate that required commands and forbidden shortcuts are present.

### Task 8: End-to-End Validation

**Objective:** Verify the upgraded workflow against `长越科技/自动化软件高级工程师` without creating candidates or sending outreach.

**Commands:**
- `python3 -m unittest discover -s /Users/messi/.codex/skills/multi-channel-search/tests -v`
- `python3 /Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py context --client 长越科技 --job 自动化软件高级工程师`
- `python3 /Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py plan --client 长越科技 --job 自动化软件高级工程师`
- `python3 /Users/messi/.codex/skills/multi-channel-search/scripts/a_system_multichannel.py preflight --client 长越科技 --job 自动化软件高级工程师 --port 9223`
- `/Users/messi/.codex/skills/a-system-workbench/scripts/a_system_sync.py --client 长越科技 --job 自动化软件高级工程师 --no-open`

**Acceptance Criteria:**
- Canonical job and profile resolve from v3.
- Historical stopped/contacted candidates appear in the exclusion summary.
- Query plan contains no role-specific hard-coded display categories.
- Login failures and generic feeds are reported as blocked, never as zero results.
- Dry-run does not change database checksums or candidate counts.
- Tests, strict client audit, and regression guard pass.
