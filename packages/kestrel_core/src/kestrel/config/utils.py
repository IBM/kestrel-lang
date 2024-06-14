import os
import sys
import yaml
from importlib import resources
from pathlib import Path
import logging
from typeguard import typechecked
from typing import Mapping, Union, Iterable

from kestrel.__future__ import is_python_older_than_minor_version
from kestrel.utils import update_nested_dict, load_data_file
from kestrel.exceptions import InvalidYamlInConfig, InvalidKestrelConfig

CONFIG_DIR_DEFAULT = Path.home() / ".config" / "kestrel"
CONFIG_PATH_DEFAULT = CONFIG_DIR_DEFAULT / "kestrel.yaml"
CONFIG_PATH_ENV_VAR = "KESTREL_CONFIG"  # override CONFIG_PATH_DEFAULT if provided

_logger = logging.getLogger(__name__)


@typechecked
def list_yaml_files_in_module(module_name: str) -> Iterable[str]:
    """List YAML files inside a Python module"""
    try:
        # resources.files() is introduced in Python 3.9
        p = resources.files(module_name)
    except AttributeError as e:
        # Python 3.8; deprecation warning forward
        if is_python_older_than_minor_version(9):
            p = Path(os.path.dirname(sys.modules[module_name].__file__))
        else:
            raise e
    return [x.name for x in p.glob("*.yaml")]


@typechecked
def load_leaf_yaml(config: Mapping, path_dir: str) -> Mapping:
    new = {}
    for k, v in config.items():
        if isinstance(v, Mapping):
            new[k] = load_leaf_yaml(v, path_dir)
        elif isinstance(v, str) and v.endswith(".yaml"):
            try:
                if os.path.isabs(v):
                    with open(v, "r") as fp:
                        new[k] = yaml.safe_load(fp.read())
                else:
                    with open(os.path.join(path_dir, v), "r") as fp:
                        new[k] = yaml.safe_load(fp.read())
            except:
                raise InvalidYamlInConfig(v)
        else:
            new[k] = v
    return new


@typechecked
def load_default_config() -> Mapping:
    _logger.debug(f"Loading default config file...")
    default_config = load_data_file("kestrel.config", "kestrel.yaml")
    config_with_envvar_expanded = os.path.expandvars(default_config)
    config_content = yaml.safe_load(config_with_envvar_expanded)
    return config_content


@typechecked
def load_user_config(
    config_path_env_var: str, config_path_default: Union[str, Path]
) -> Mapping:
    config_path_default = config_path_default.absolute().as_posix()
    config_path = os.getenv(config_path_env_var, config_path_default)
    config_path = os.path.expanduser(config_path)
    config = {}
    if config_path:
        try:
            with open(config_path, "r") as fp:
                _logger.debug(f"User configuration file found: {config_path}")
                config = yaml.safe_load(os.path.expandvars(fp.read()))
            config = load_leaf_yaml(config, os.path.dirname(config_path))
        except FileNotFoundError:
            _logger.debug(f"User configuration file not exist.")
    return config


@typechecked
def load_kestrel_config() -> Mapping:
    config_default = load_default_config()
    config_user = load_user_config(CONFIG_PATH_ENV_VAR, CONFIG_PATH_DEFAULT)
    _logger.debug(f"User configuration loaded: {config_user}")
    _logger.debug(f"Updating default config with user config...")
    full_config = update_nested_dict(config_default, config_user)

    # valid the entity identifier section format
    for entity, idx in full_config["entity_identifier"].items():
        if not (isinstance(idx, list) and all((isinstance(x, str) for x in idx))):
            raise InvalidKestrelConfig(f"Invalid entity_identifier for '{entity}'")

    return full_config
