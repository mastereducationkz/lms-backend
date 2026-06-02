-- Analytics performance indexes (safe to run multiple times)

CREATE INDEX IF NOT EXISTS idx_step_progress_course_status_completed
ON step_progress (course_id, status, completed_at)

CREATE INDEX IF NOT EXISTS idx_step_progress_user_course_status
ON step_progress (user_id, course_id, status)

CREATE INDEX IF NOT EXISTS idx_step_progress_user_visited
ON step_progress (user_id, visited_at)

CREATE INDEX IF NOT EXISTS idx_group_students_group_id
ON group_students (group_id)

CREATE INDEX IF NOT EXISTS idx_group_students_student_id
ON group_students (student_id)

CREATE INDEX IF NOT EXISTS idx_enrollments_course_active
ON enrollments (course_id, is_active)

CREATE INDEX IF NOT EXISTS idx_enrollments_user_active
ON enrollments (user_id, is_active)

CREATE INDEX IF NOT EXISTS idx_assignment_submissions_user_assignment
ON assignment_submissions (user_id, assignment_id)

CREATE INDEX IF NOT EXISTS idx_assignment_submissions_assignment_submitted
ON assignment_submissions (assignment_id, submitted_at)

CREATE INDEX IF NOT EXISTS idx_assignment_submissions_user_graded
ON assignment_submissions (user_id, is_graded)

