"""Platform Integration Pack — LMS side (see Master.IELTS docs/superpowers/specs/2026-09-03-platform-integration-pack.md).

The exam platforms (IELTS, SAT/NUET) push domain events to the LMS (``POST /integrations/events``);
the LMS keeps a replayable copy (``platform_events``) and projects it into ``platform_results`` /
``platform_weekly_sets``. Platforms remain the source of truth; a nightly job reconciles from
their batch-scores endpoints so a lost event never leaves a hole.
"""
