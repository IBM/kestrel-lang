from kestrel.config.utils import load_kestrel_config, list_yaml_files_in_module
import os


def test_env_vars_in_config():

    test_config = """---
credentials:
  username: $TEST_USER
  password: $TEST_PASSWORD
    """
    os.environ["TEST_USER"] = "test-user"
    os.environ["TEST_PASSWORD"] = "test-password"
    os.environ["KESTREL_CONFIG"] = os.path.join(os.sep, "tmp", "config.yaml")

    with open(os.getenv("KESTREL_CONFIG"), "w") as fp:
        fp.write(test_config)
    config = load_kestrel_config()
    assert config["credentials"]["username"] == "test-user"
    assert config["credentials"]["password"] == "test-password"


def test_env_vars_in_config_overwrite():

    test_config = """---
credentials:
  username: ${TEST_USER}
  password: ${TEST_PASSWORD}
debug:
  cache_directory_prefix: $KESTREL_CACHE_DIRECTORY_PREFIX
    """
    os.environ["TEST_USER"] = "test-user"
    os.environ["TEST_PASSWORD"] = "test-password"
    os.environ["KESTREL_CONFIG"] = os.path.join(os.sep, "tmp", "config.yaml")
    os.environ["KESTREL_CACHE_DIRECTORY_PREFIX"] = "Kestrel2.0-"
    with open(os.getenv("KESTREL_CONFIG"), "w") as fp:
        fp.write(test_config)
    config = load_kestrel_config()
    assert config["credentials"]["username"] == "test-user"
    assert config["credentials"]["password"] == "test-password"
    assert config["debug"]["cache_directory_prefix"] == "Kestrel2.0-"

def test_empty_env_var_in_config():
    test_config = """---
credentials:
  username: ${TEST_USER}
  password: ${TEST_PASSWORD}
debug:
  cache_directory_prefix: $I_DONT_EXIST
    """
    os.environ["TEST_USER"] = "test-user"
    os.environ["TEST_PASSWORD"] = "test-password"
    os.environ["KESTREL_CONFIG"] = os.path.join(os.sep, "tmp", "config.yaml")
    os.environ["KESTREL_CACHE_DIRECTORY_PREFIX"] = "Kestrel2.0-"
    with open(os.getenv("KESTREL_CONFIG"), "w") as fp:
        fp.write(test_config)
    config = load_kestrel_config()
    assert config["credentials"]["username"] == "test-user"
    assert config["credentials"]["password"] == "test-password"
    assert config["debug"]["cache_directory_prefix"] == "$I_DONT_EXIST"

def test_yaml_load_in_config(tmp_path):
    test_config = """---
credentials:
  username: ${TEST_USER}
  password: ${TEST_PASSWORD}
loadtest:
  xyz:
    abc: abc.yaml
    """
    os.environ["TEST_USER"] = "test-user"
    os.environ["TEST_PASSWORD"] = "test-password"
    os.environ["KESTREL_CONFIG"] = os.path.join(tmp_path, "config.yaml")
    with open(os.getenv("KESTREL_CONFIG"), "w") as fp:
        fp.write(test_config)
    with open(os.path.join(tmp_path, "abc.yaml"), "w") as fp:
        fp.write("test: fake-value")
    config = load_kestrel_config()
    assert config["credentials"]["username"] == "test-user"
    assert config["credentials"]["password"] == "test-password"
    assert config["loadtest"]["xyz"]["abc"]["test"] == "fake-value"

def test_list_yaml_files():
    xs = list_yaml_files_in_module("kestrel.mapping.entityname")
    assert set(xs) == set(("alias.yaml", "ecs.yaml", "stix.yaml"))
