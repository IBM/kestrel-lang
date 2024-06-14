import logging
from dataclasses import dataclass
from typing import Dict, Optional

from mashumaro.mixins.json import DataClassJSONMixin

from kestrel.config.utils import (
    CONFIG_DIR_DEFAULT,
    load_user_config,
    load_kestrel_config,
)
from kestrel.exceptions import InterfaceNotConfigured
from kestrel.mapping.data_model import (
    load_default_mapping,
    check_entity_identifier_existence_in_mapping,
)


PROFILE_PATH_DEFAULT = CONFIG_DIR_DEFAULT / "sqlalchemy.yaml"
PROFILE_PATH_ENV_VAR = "KESTREL_SQLALCHEMY_CONFIG"

_logger = logging.getLogger(__name__)


@dataclass
class Connection(DataClassJSONMixin):
    url: str  # SQLAlchemy "connection URL" or "connection string"


@dataclass
class DataSource(DataClassJSONMixin):
    connection: str
    table: str
    timestamp: str
    timestamp_format: str
    data_model_map: Optional[Dict] = None
    entity_identifier: Optional[Dict] = None

    def __post_init__(self):
        if not self.data_model_map:
            # Default to the built-in ECS mapping
            self.data_model_map = load_default_mapping("ecs")  # FIXME: need a default?

        kestrel_config = load_kestrel_config()
        check_entity_identifier_existence_in_mapping(
            self.data_model_map,
            kestrel_config["entity_identifier"],
            "sqlalchemy interface",
        )


@dataclass
class Config(DataClassJSONMixin):
    connections: Dict[str, Connection]
    datasources: Dict[str, DataSource]

    def __post_init__(self):
        self.connections = {k: Connection(**v) for k, v in self.connections.items()}
        self.datasources = {k: DataSource(**v) for k, v in self.datasources.items()}


def load_config():
    try:
        interface_config = Config(
            **load_user_config(PROFILE_PATH_ENV_VAR, PROFILE_PATH_DEFAULT)
        )
        return interface_config
    except TypeError:
        raise InterfaceNotConfigured()
