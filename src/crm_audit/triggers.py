"""Postgres triggers that enqueue CRM audit events inside the mutating transaction.

Why triggers rather than application code: the LMS mutates groups, lessons, membership and
attendance from many places — `src/admin/routes/admin.py`, `src/events/routes/events.py`,
the bulk schedule upload, the CRM's own internal write endpoints — and several of them issue
raw SQL. Instrumenting every call site guarantees the one that is added next week is missed.
The table is the one thing they all go through.

Every trigger body is wrapped in ``EXCEPTION WHEN OTHERS``, matching `student_sync`. That is
deliberate and it is the brief's rule 10: an audit-bookkeeping failure must never roll back
or 500 a domain mutation that is otherwise fine. The INSERT still happens *in the caller's
transaction*, so a mutation that commits has its event — the shield only covers catastrophic
cases like schema drift removing a referenced column.

The payload shape is the CRM's ``IngestEvent``. ``group_ids`` is resolved here, at the source,
because only this database knows which groups a lesson belongs to.
"""

#: Sent as `actor.kind`. The LMS does not know CRM user ids, so the CRM records the numeric
#: id as `lms_actor_id` rather than mistaking it for one of its own.
ACTOR_KIND = "system"

GROUP_FUNCTION = "crm_audit_enqueue_group"
GROUP_TRIGGER = "trg_crm_audit_group"

MEMBER_FUNCTION = "crm_audit_enqueue_member"
MEMBER_TRIGGER = "trg_crm_audit_member"

EVENT_FUNCTION = "crm_audit_enqueue_event"
EVENT_TRIGGER = "trg_crm_audit_event"

ATTENDANCE_FUNCTION = "crm_audit_enqueue_attendance"
ATTENDANCE_TRIGGER = "trg_crm_audit_attendance"

ALL_FUNCTIONS = (GROUP_FUNCTION, MEMBER_FUNCTION, EVENT_FUNCTION, ATTENDANCE_FUNCTION)
ALL_TRIGGERS = (
    (GROUP_TRIGGER, "groups"),
    (MEMBER_TRIGGER, "group_students"),
    (EVENT_TRIGGER, "events"),
    (ATTENDANCE_TRIGGER, "attendances"),
)


# --- groups -------------------------------------------------------------------------------

GROUP_TRIGGER_SQL = f"""
CREATE OR REPLACE FUNCTION {GROUP_FUNCTION}() RETURNS trigger AS $fn$
DECLARE
    v_event_id text;
    v_action   text;
    v_summary  text;
    v_before   json;
    v_after    json;
BEGIN
    BEGIN
        IF (TG_OP = 'INSERT') THEN
            v_action := 'group.created';
            v_summary := 'Группа создана в LMS: ' || coalesce(NEW.name, '#' || NEW.id);
            v_before := NULL;
            v_after := json_build_object(
                'name', NEW.name, 'teacher_id', NEW.teacher_id, 'curator_id', NEW.curator_id,
                'program_type', NEW.program_type, 'group_type', NEW.group_type,
                'is_active', NEW.is_active
            );
        ELSE
            -- Only structural fields. Without this guard every unrelated column write would
            -- enqueue an event whose diff is empty.
            IF NOT (
                NEW.name IS DISTINCT FROM OLD.name
                OR NEW.teacher_id IS DISTINCT FROM OLD.teacher_id
                OR NEW.curator_id IS DISTINCT FROM OLD.curator_id
                OR NEW.program_type IS DISTINCT FROM OLD.program_type
                OR NEW.group_type IS DISTINCT FROM OLD.group_type
                OR NEW.is_active IS DISTINCT FROM OLD.is_active
                OR NEW.is_over IS DISTINCT FROM OLD.is_over
            ) THEN
                RETURN NULL;
            END IF;
            v_action := 'group.updated';
            v_summary := 'Группа изменена в LMS: ' || coalesce(NEW.name, '#' || NEW.id);
            v_before := json_build_object(
                'name', OLD.name, 'teacher_id', OLD.teacher_id, 'curator_id', OLD.curator_id,
                'program_type', OLD.program_type, 'group_type', OLD.group_type,
                'is_active', OLD.is_active, 'is_over', OLD.is_over
            );
            v_after := json_build_object(
                'name', NEW.name, 'teacher_id', NEW.teacher_id, 'curator_id', NEW.curator_id,
                'program_type', NEW.program_type, 'group_type', NEW.group_type,
                'is_active', NEW.is_active, 'is_over', NEW.is_over
            );
        END IF;

        v_event_id := gen_random_uuid()::text;
        INSERT INTO crm_audit_outbox (event_id, action, payload, status, attempts, created_at)
        VALUES (
            v_event_id, v_action,
            json_build_object(
                'event_id', v_event_id,
                'action', v_action,
                'entity_type', 'group',
                'entity_id', NEW.id,
                'summary', v_summary,
                'occurred_at', to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                'group_ids', json_build_array(NEW.id),
                'actor', json_build_object('kind', '{ACTOR_KIND}'),
                'before', v_before,
                'after', v_after
            ),
            'pending', 0, now()
        );
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'crm_audit group trigger failed: %', SQLERRM;
    END;
    RETURN NULL;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS {GROUP_TRIGGER} ON groups;
CREATE TRIGGER {GROUP_TRIGGER}
AFTER INSERT OR UPDATE ON groups
FOR EACH ROW EXECUTE FUNCTION {GROUP_FUNCTION}();
"""


# --- membership ---------------------------------------------------------------------------

MEMBER_TRIGGER_SQL = f"""
CREATE OR REPLACE FUNCTION {MEMBER_FUNCTION}() RETURNS trigger AS $fn$
DECLARE
    v_event_id  text;
    v_action    text;
    v_group_id  integer;
    v_student   integer;
    v_name      text;
    v_group     text;
BEGIN
    BEGIN
        IF (TG_OP = 'DELETE') THEN
            v_action := 'student.group.removed';
            v_group_id := OLD.group_id;
            v_student := OLD.student_id;
        ELSE
            v_action := 'student.group.added';
            v_group_id := NEW.group_id;
            v_student := NEW.student_id;
        END IF;

        SELECT name INTO v_name FROM users WHERE id = v_student;
        SELECT name INTO v_group FROM groups WHERE id = v_group_id;

        v_event_id := gen_random_uuid()::text;
        INSERT INTO crm_audit_outbox (event_id, action, payload, status, attempts, created_at)
        VALUES (
            v_event_id, v_action,
            json_build_object(
                'event_id', v_event_id,
                'action', v_action,
                'entity_type', 'student_account',
                'summary', coalesce(v_name, '#' || v_student)
                    || CASE WHEN v_action = 'student.group.added'
                            THEN ' добавлен(а) в группу «' ELSE ' удалён(а) из группы «' END
                    || coalesce(v_group, '#' || v_group_id) || '» в LMS',
                'occurred_at', to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                'group_ids', json_build_array(v_group_id),
                'lms_student_id', v_student,
                'actor', json_build_object('kind', '{ACTOR_KIND}'),
                'after', json_build_object('group_id', v_group_id, 'group_name', v_group)
            ),
            'pending', 0, now()
        );
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'crm_audit member trigger failed: %', SQLERRM;
    END;
    RETURN NULL;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS {MEMBER_TRIGGER} ON group_students;
CREATE TRIGGER {MEMBER_TRIGGER}
AFTER INSERT OR DELETE ON group_students
FOR EACH ROW EXECUTE FUNCTION {MEMBER_FUNCTION}();
"""


# --- lessons ------------------------------------------------------------------------------
#
# `group_ids` is aggregated from `event_groups` here rather than left for the CRM to resolve:
# a lesson taught to three groups is one event belonging to all three histories, and only
# this database knows which three.

EVENT_TRIGGER_SQL = f"""
CREATE OR REPLACE FUNCTION {EVENT_FUNCTION}() RETURNS trigger AS $fn$
DECLARE
    v_event_id text;
    v_action   text;
    v_groups   json;
    v_before   json;
    v_after    json;
BEGIN
    BEGIN
        IF (NEW.event_type IS DISTINCT FROM 'class') THEN
            RETURN NULL;
        END IF;

        IF (TG_OP = 'INSERT') THEN
            v_action := 'lesson.created';
            v_before := NULL;
            v_after := json_build_object(
                'title', NEW.title, 'start_datetime', NEW.start_datetime,
                'end_datetime', NEW.end_datetime, 'teacher_id', NEW.teacher_id
            );
        ELSE
            IF NOT (
                NEW.start_datetime IS DISTINCT FROM OLD.start_datetime
                OR NEW.end_datetime IS DISTINCT FROM OLD.end_datetime
                OR NEW.teacher_id IS DISTINCT FROM OLD.teacher_id
                OR NEW.title IS DISTINCT FROM OLD.title
                OR NEW.is_active IS DISTINCT FROM OLD.is_active
            ) THEN
                RETURN NULL;
            END IF;
            -- Cancellation and restoration are their own actions: "the lesson was cancelled"
            -- is a different fact from "the lesson was edited", and the group history reads
            -- them under different filters.
            IF (OLD.is_active IS DISTINCT FROM NEW.is_active) THEN
                v_action := CASE WHEN NEW.is_active THEN 'lesson.restored' ELSE 'lesson.cancelled' END;
            ELSE
                v_action := 'lesson.updated';
            END IF;
            v_before := json_build_object(
                'title', OLD.title, 'start_datetime', OLD.start_datetime,
                'end_datetime', OLD.end_datetime, 'teacher_id', OLD.teacher_id,
                'is_active', OLD.is_active
            );
            v_after := json_build_object(
                'title', NEW.title, 'start_datetime', NEW.start_datetime,
                'end_datetime', NEW.end_datetime, 'teacher_id', NEW.teacher_id,
                'is_active', NEW.is_active
            );
        END IF;

        SELECT coalesce(json_agg(eg.group_id), '[]'::json) INTO v_groups
        FROM event_groups eg WHERE eg.event_id = NEW.id;

        v_event_id := gen_random_uuid()::text;
        INSERT INTO crm_audit_outbox (event_id, action, payload, status, attempts, created_at)
        VALUES (
            v_event_id, v_action,
            json_build_object(
                'event_id', v_event_id,
                'action', v_action,
                'entity_type', 'lesson',
                'entity_id', NEW.id,
                'lms_event_id', NEW.id,
                'summary', 'Урок «' || coalesce(NEW.title, '#' || NEW.id) || '» изменён в LMS',
                'occurred_at', to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                'group_ids', v_groups,
                'actor', json_build_object('kind', '{ACTOR_KIND}'),
                'before', v_before,
                'after', v_after
            ),
            'pending', 0, now()
        );
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'crm_audit event trigger failed: %', SQLERRM;
    END;
    RETURN NULL;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS {EVENT_TRIGGER} ON events;
CREATE TRIGGER {EVENT_TRIGGER}
AFTER INSERT OR UPDATE ON events
FOR EACH ROW EXECUTE FUNCTION {EVENT_FUNCTION}();
"""


# --- attendance ---------------------------------------------------------------------------

ATTENDANCE_TRIGGER_SQL = f"""
CREATE OR REPLACE FUNCTION {ATTENDANCE_FUNCTION}() RETURNS trigger AS $fn$
DECLARE
    v_event_id text;
    v_groups   json;
BEGIN
    BEGIN
        IF (TG_OP = 'UPDATE') AND NOT (NEW.status IS DISTINCT FROM OLD.status) THEN
            RETURN NULL;
        END IF;

        SELECT coalesce(json_agg(eg.group_id), '[]'::json) INTO v_groups
        FROM event_groups eg WHERE eg.event_id = NEW.event_id;

        v_event_id := gen_random_uuid()::text;
        INSERT INTO crm_audit_outbox (event_id, action, payload, status, attempts, created_at)
        VALUES (
            v_event_id, 'lesson.attendance.marked',
            json_build_object(
                'event_id', v_event_id,
                'action', 'lesson.attendance.marked',
                'entity_type', 'lesson',
                'entity_id', NEW.event_id,
                'lms_event_id', NEW.event_id,
                'lms_student_id', NEW.user_id,
                'summary', 'Посещаемость отмечена в LMS: ' || coalesce(NEW.status, '—'),
                'occurred_at', to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                'group_ids', v_groups,
                'actor', json_build_object('kind', '{ACTOR_KIND}'),
                'before', CASE WHEN TG_OP = 'UPDATE'
                               THEN json_build_object('status', OLD.status) ELSE NULL END,
                'after', json_build_object('status', NEW.status)
            ),
            'pending', 0, now()
        );
    EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'crm_audit attendance trigger failed: %', SQLERRM;
    END;
    RETURN NULL;
END;
$fn$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS {ATTENDANCE_TRIGGER} ON attendances;
CREATE TRIGGER {ATTENDANCE_TRIGGER}
AFTER INSERT OR UPDATE ON attendances
FOR EACH ROW EXECUTE FUNCTION {ATTENDANCE_FUNCTION}();
"""


ALL_TRIGGER_SQL = (
    GROUP_TRIGGER_SQL,
    MEMBER_TRIGGER_SQL,
    EVENT_TRIGGER_SQL,
    ATTENDANCE_TRIGGER_SQL,
)


def install_sql() -> str:
    """Every trigger, idempotently — `CREATE OR REPLACE` plus `DROP TRIGGER IF EXISTS`."""
    return "\n".join(ALL_TRIGGER_SQL)


def uninstall_sql() -> str:
    statements = [f"DROP TRIGGER IF EXISTS {t} ON {table};" for t, table in ALL_TRIGGERS]
    statements += [f"DROP FUNCTION IF EXISTS {fn}();" for fn in ALL_FUNCTIONS]
    return "\n".join(statements)
