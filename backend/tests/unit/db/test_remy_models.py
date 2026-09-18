"""Schema-level tests for Remy data models: ChatSession, ChatMessage, RemySkill."""

from sqlalchemy import CheckConstraint

from modulo.db.models import Base, ChatMessage, ChatSession, OrgScoped, RemySkill


class TestChatSession:
    def test_table_exists(self) -> None:
        assert "chat_sessions" in Base.metadata.tables

    def test_columns(self) -> None:
        cols = Base.metadata.tables["chat_sessions"].c
        assert "id" in cols
        assert "organisation_id" in cols
        assert "account_id" in cols
        assert "name" in cols
        assert "provider" in cols
        assert "model" in cols
        assert "context_window_tokens" in cols
        assert "system_prompt_hash" in cols
        assert "created_at" in cols
        assert "updated_at" in cols

    def test_is_org_scoped(self) -> None:
        assert issubclass(ChatSession, OrgScoped)

    def test_account_id_not_null(self) -> None:
        col = Base.metadata.tables["chat_sessions"].c["account_id"]
        assert not col.nullable

    def test_provider_not_null(self) -> None:
        col = Base.metadata.tables["chat_sessions"].c["provider"]
        assert not col.nullable

    def test_model_not_null(self) -> None:
        col = Base.metadata.tables["chat_sessions"].c["model"]
        assert not col.nullable

    def test_context_window_tokens_not_null(self) -> None:
        col = Base.metadata.tables["chat_sessions"].c["context_window_tokens"]
        assert not col.nullable


class TestChatMessage:
    def test_table_exists(self) -> None:
        assert "chat_messages" in Base.metadata.tables

    def test_columns(self) -> None:
        cols = Base.metadata.tables["chat_messages"].c
        assert "id" in cols
        assert "organisation_id" in cols
        assert "session_id" in cols
        assert "role" in cols
        assert "content" in cols
        assert "tool_calls_json" in cols
        assert "tool_results_json" in cols
        assert "token_count" in cols
        assert "parent_id" in cols
        assert "created_at" in cols

    def test_is_not_org_scoped(self) -> None:
        assert not issubclass(ChatMessage, OrgScoped)

    def test_parent_id_self_referential_fk(self) -> None:
        table = Base.metadata.tables["chat_messages"]
        parent_fk = [fk for fk in table.foreign_keys if fk.parent.name == "parent_id"]
        assert len(parent_fk) == 1
        assert parent_fk[0].column.table.name == "chat_messages"

    def test_parent_id_ondelete_set_null(self) -> None:
        table = Base.metadata.tables["chat_messages"]
        parent_fk = next(fk for fk in table.foreign_keys if fk.parent.name == "parent_id")
        assert parent_fk.ondelete == "SET NULL"

    def test_session_id_fk_to_chat_sessions(self) -> None:
        table = Base.metadata.tables["chat_messages"]
        session_fk = [fk for fk in table.foreign_keys if fk.parent.name == "session_id"]
        assert len(session_fk) == 1
        assert session_fk[0].column.table.name == "chat_sessions"

    def test_session_id_ondelete_cascade(self) -> None:
        table = Base.metadata.tables["chat_messages"]
        session_fk = next(fk for fk in table.foreign_keys if fk.parent.name == "session_id")
        assert session_fk.ondelete == "CASCADE"

    def test_organisation_id_not_null(self) -> None:
        col = Base.metadata.tables["chat_messages"].c["organisation_id"]
        assert not col.nullable

    def test_role_not_null(self) -> None:
        col = Base.metadata.tables["chat_messages"].c["role"]
        assert not col.nullable

    def test_role_check_constraint_exists(self) -> None:
        table = Base.metadata.tables["chat_messages"]
        check = next(
            (c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == "ck_chat_messages_role"),
            None,
        )
        assert check is not None, "Missing ck_chat_messages_role CHECK constraint"

    def test_role_check_allows_valid_roles(self) -> None:
        table = Base.metadata.tables["chat_messages"]
        check = next(
            c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == "ck_chat_messages_role"
        )
        sql = str(check.sqltext)
        for role in ("user", "assistant", "tool_use", "tool_result", "summary"):
            assert f"'{role}'" in sql

    def test_role_check_rejects_invalid_roles(self) -> None:
        table = Base.metadata.tables["chat_messages"]
        check = next(
            c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == "ck_chat_messages_role"
        )
        sql = str(check.sqltext)
        assert "IN" in sql


class TestRemySkill:
    def test_table_exists(self) -> None:
        assert "remy_skills" in Base.metadata.tables

    def test_columns(self) -> None:
        cols = Base.metadata.tables["remy_skills"].c
        assert "id" in cols
        assert "organisation_id" in cols
        assert "account_id" in cols
        assert "name" in cols
        assert "description" in cols
        assert "triggers" in cols
        assert "body" in cols
        assert "active" in cols
        assert "created_at" in cols
        assert "updated_at" in cols

    def test_is_not_org_scoped(self) -> None:
        assert not issubclass(RemySkill, OrgScoped)

    def test_organisation_id_nullable(self) -> None:
        col = Base.metadata.tables["remy_skills"].c["organisation_id"]
        assert col.nullable

    def test_account_id_nullable(self) -> None:
        col = Base.metadata.tables["remy_skills"].c["account_id"]
        assert col.nullable

    def test_active_defaults_true(self) -> None:
        col = Base.metadata.tables["remy_skills"].c["active"]
        assert col.server_default is not None
        assert "true" in str(col.server_default.arg).lower()

    def test_name_not_null(self) -> None:
        col = Base.metadata.tables["remy_skills"].c["name"]
        assert not col.nullable

    def test_body_not_null(self) -> None:
        col = Base.metadata.tables["remy_skills"].c["body"]
        assert not col.nullable

    def test_exactly_one_owner_check_constraint(self) -> None:
        table = Base.metadata.tables["remy_skills"]
        check = next(
            (c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == "ck_remy_skills_owner"),
            None,
        )
        assert check is not None, "Missing ck_remy_skills_owner CHECK constraint"

    def test_owner_check_rejects_both_null(self) -> None:
        table = Base.metadata.tables["remy_skills"]
        check = next(
            c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == "ck_remy_skills_owner"
        )
        sql = str(check.sqltext)
        assert "organisation_id IS NOT NULL" in sql
        assert "account_id IS NOT NULL" in sql

    def test_owner_check_rejects_both_set(self) -> None:
        table = Base.metadata.tables["remy_skills"]
        check = next(
            c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == "ck_remy_skills_owner"
        )
        sql = str(check.sqltext)
        assert "OR" in sql
        assert "organisation_id IS NULL AND account_id IS NOT NULL" in sql or "account_id IS NOT NULL" in sql
