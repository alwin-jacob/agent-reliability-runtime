"""Independent package and durable-schema version types and constants."""

from typing import Final, Literal, TypeAlias

PackageVersion: TypeAlias = Literal["0.3.2"]
ArtifactSchemaVersion: TypeAlias = Literal["0.3.0"]
TaskSchemaVersion: TypeAlias = Literal["0.2.0"]
RunConfigSchemaVersion: TypeAlias = Literal["0.2.0"]
ModelFixtureSchemaVersion: TypeAlias = Literal["0.2.0"]
OrderFixtureSchemaVersion: TypeAlias = Literal["0.2.0"]
PolicyFixtureSchemaVersion: TypeAlias = Literal["0.2.0"]

PACKAGE_VERSION: Final[PackageVersion] = "0.3.2"
ARTIFACT_SCHEMA_VERSION: Final[ArtifactSchemaVersion] = "0.3.0"
TASK_SCHEMA_VERSION: Final[TaskSchemaVersion] = "0.2.0"
RUN_CONFIG_SCHEMA_VERSION: Final[RunConfigSchemaVersion] = "0.2.0"
MODEL_FIXTURE_SCHEMA_VERSION: Final[ModelFixtureSchemaVersion] = "0.2.0"
ORDER_FIXTURE_SCHEMA_VERSION: Final[OrderFixtureSchemaVersion] = "0.2.0"
POLICY_FIXTURE_SCHEMA_VERSION: Final[PolicyFixtureSchemaVersion] = "0.2.0"
