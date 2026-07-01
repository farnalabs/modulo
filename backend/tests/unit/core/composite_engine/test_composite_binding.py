"""Tests for CompositeBinding schema."""

import uuid

from modulo.core.composite_engine.composite_binding import CompositeBinding


class TestCompositeBinding:
    def test_minimal_binding(self) -> None:
        tid = uuid.uuid4()
        binding = CompositeBinding(
            composite_template_id=tid,
            composite_version="1.0.0",
        )
        assert binding.composite_template_id == tid
        assert binding.composite_version == "1.0.0"
        assert binding.parameter_values == {}
        assert binding.input_mapping is None
        assert binding.output_mapping is None

    def test_binding_with_parameters(self) -> None:
        tid = uuid.uuid4()
        binding = CompositeBinding(
            composite_template_id=tid,
            composite_version="2.1.0",
            parameter_values={"model": "gpt-4", "temperature": 0.3},
            input_mapping={"target": "source.field"},
            output_mapping={"result": "output.data"},
        )
        assert binding.parameter_values == {"model": "gpt-4", "temperature": 0.3}
        assert binding.input_mapping == {"target": "source.field"}
        assert binding.output_mapping == {"result": "output.data"}

    def test_serialization_roundtrip(self) -> None:
        tid = uuid.uuid4()
        binding = CompositeBinding(
            composite_template_id=tid,
            composite_version="1.5.0",
            parameter_values={"key": "val"},
        )
        data = binding.model_dump(mode="json")
        restored = CompositeBinding.model_validate(data)
        assert restored.composite_template_id == tid
        assert restored.composite_version == "1.5.0"
        assert restored.parameter_values == {"key": "val"}
