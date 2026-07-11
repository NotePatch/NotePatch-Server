from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


def metadata_field(default_factory=dict):
    return Field(
        default_factory=default_factory,
        validation_alias="metadata_",
        serialization_alias="metadata",
    )
