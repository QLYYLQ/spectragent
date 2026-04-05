"""Tests for Environment registrar and EnvironmentBase."""

from spectragent.core.environments._registrar import EnvironmentBase, EnvironmentRegistrar


class TestEnvironmentRegistration:
    def test_concrete_env_registers(self) -> None:
        class TestEnv(EnvironmentBase):
            __registry_key__ = "test_reg_env"

        assert EnvironmentRegistrar.get("test_reg_env") is TestEnv
        EnvironmentRegistrar.unregister("test_reg_env")

    def test_abstract_not_registered(self) -> None:
        class AbstractEnv(EnvironmentBase):
            __abstract__ = True

        assert EnvironmentRegistrar.get_or_none("abstract_env") is None

    def test_env_type_property(self) -> None:
        class TypeEnv(EnvironmentBase):
            __registry_key__ = "test_type_env"

        instance = TypeEnv(capabilities={})
        assert instance.env_type == "test_type_env"
        EnvironmentRegistrar.unregister("test_type_env")
