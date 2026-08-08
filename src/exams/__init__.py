"""Exam results domain.

Holds normalized official exam results (SAT / IELTS / NUET) and the queryable
projection of Bluebook practice-test results that backs the group progress grid.

Planned/expected exam dates deliberately do NOT live here - they remain on
``AssignmentZeroSubmission.sat_planned_test_date`` / ``ielts_planned_test_date``,
which is the platform's established source of truth. This domain records what a
student ACTUALLY scored; Assignment Zero records what they intend to sit.
"""
