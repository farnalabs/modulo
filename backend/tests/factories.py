"""Factory Boy factories for test entity creation."""

import uuid

import factory

from modulo.db.models import Account, Organisation, Pipeline, PipelineSnapshot, Run


class OrganisationFactory(factory.alchemy.SQLAlchemyModelFactory):
    class Meta:
        model = Organisation
        sqlalchemy_session_persistence = "flush"

    id = factory.LazyFunction(uuid.uuid4)
    name = factory.Sequence(lambda n: f"Test Org {n}")
    slug = factory.Sequence(lambda n: f"test-org-{n}")
    status = "active"
    settings_json = factory.LazyFunction(dict)


class AccountFactory(factory.alchemy.SQLAlchemyModelFactory):
    class Meta:
        model = Account
        sqlalchemy_session_persistence = "flush"

    id = factory.LazyFunction(uuid.uuid4)
    email = factory.Sequence(lambda n: f"user{n}@example.com")
    display_name = factory.Sequence(lambda n: f"Test User {n}")
    auth_provider = "local"
    active = True


class PipelineFactory(factory.alchemy.SQLAlchemyModelFactory):
    class Meta:
        model = Pipeline
        sqlalchemy_session_persistence = "flush"

    id = factory.LazyFunction(uuid.uuid4)
    name = factory.Sequence(lambda n: f"Pipeline {n}")
    creator = factory.SubFactory(AccountFactory)
    organisation = factory.SubFactory(OrganisationFactory)
    visibility = "org"
    run_context_defaults = factory.LazyFunction(dict)


class PipelineSnapshotFactory(factory.alchemy.SQLAlchemyModelFactory):
    class Meta:
        model = PipelineSnapshot
        sqlalchemy_session_persistence = "flush"

    id = factory.LazyFunction(uuid.uuid4)
    pipeline = factory.SubFactory(PipelineFactory)
    organisation = factory.SelfAttribute("pipeline.organisation")
    snapshot_version = factory.Sequence(lambda n: n + 1)
    graph_json = factory.LazyFunction(dict)
    connector_bindings_json = factory.LazyFunction(list)
    schema_pins_json = factory.LazyFunction(list)
    prompt_pins_json = factory.LazyFunction(list)
    model_backend_pins_json = factory.LazyFunction(list)
    parameter_bindings_json = None
    run_context_defaults = factory.LazyFunction(dict)


class RunFactory(factory.alchemy.SQLAlchemyModelFactory):
    class Meta:
        model = Run
        sqlalchemy_session_persistence = "flush"

    id = factory.LazyFunction(uuid.uuid4)
    snapshot = factory.SubFactory(PipelineSnapshotFactory)
    pipeline = factory.SelfAttribute("snapshot.pipeline")
    organisation = factory.SelfAttribute("snapshot.organisation")
    status = "pending"
    trigger_type = "manual"
    input_hash = "0" * 64
    langgraph_thread_id = factory.LazyFunction(lambda: str(uuid.uuid4()))
