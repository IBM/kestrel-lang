import pytest

import pandas as pd

from kestrel.exceptions import IncompleteDataMapping
from kestrel.config.utils import load_kestrel_config
from kestrel.mapping.data_model import (
    load_default_mapping,
    reverse_mapping,
    translate_comparison_to_native,
    translate_comparison_to_ocsf,
    translate_dataframe,
    translate_projection_to_native,
    check_entity_identifier_existence_in_mapping,
)


# A "custom" mapping for an opensearch/elasticsearch datasource.
# This mapping works with data from Blue Team Village's 2023 DefCon CTF, for example.
WINLOGBEAT_MAPPING = {
    "file": {
        "path": "file.path",
        "name": "file.name"
    },
    "process": {
        "cmd_line": "winlog.event_data.CommandLine",
        "pid": {
            "native_field": "winlog.event_data.ProcessId",
            "native_value": "to_str",
            "ocsf_value": "to_int"
        },
        "uid": "winlog.event_data.ProcessGuid",
        "file": {
            "path": "winlog.event_data.Image",
            "name": [
                {
                    "native_field": "winlog.event_data.Image",
                    "native_op": "LIKE",
                    "native_value": "winpath_endswith",
                    "ocsf_value": "basename"
                },
                {   # Only for testing - winlogbeat will only use windows paths
                    "native_field": "winlog.event_data.Image",
                    "native_op": "LIKE",
                    "native_value": "posixpath_endswith",
                    "ocsf_value": "basename"
                }
            ],
            "parent_folder": [
                {
                    "native_field": "winlog.event_data.Image",
                    "native_op": "LIKE",
                    "native_value": "winpath_startswith",
                    "ocsf_value": "dirname"
                }
            ]
        },
        "parent_process": {
            "cmd_line": "winlog.event_data.ParentCommandLine",
            "pid": "winlog.event_data.ParentProcessId",
            "uid": "winlog.event_data.ParentProcessGuid",
            "file": {
                "path": "winlog.event_data.ParentImage",
                "name": [
                    {
                        "native_field": "winlog.event_data.ParentImage",
                        "native_op": "LIKE",
                        "native_value": "winpath_endswith",
                        "ocsf_value": "basename"
                    }
                ],
                "parent_folder": [
                    {
                        "native_field": "winlog.event_data.ParentImage",
                        "native_op": "LIKE",
                        "native_value": "winpath_startswith",
                        "ocsf_value": "dirname"
                    }
                ]
            }
        }
    },
    "dst_endpoint": {
        "ip": "winlog.event_data.DestinationIp",
        "port": "winlog.event_data.DestinationPort"
    },
    "src_endpoint": {
        "ip": "winlog.event_data.SourceIp",
        "port": "winlog.event_data.SourcePort"
    }
}


# Mapping for testing missing identifier
INCOMPLETE_MAPPING = {
    "process": {
        "pid": "process.pid"
    }
}


# Simplified subset of the standard mapping
STIX_MAPPING = {
    "device": {
        "ip": "ipv4-addr:value"
    },
    "endpoint": {
        "ip": "ipv4-addr:value"
    },
}


# This mapping is used in 2 places:
# - frontend comparison from ECS to OCSF
# - backend comparison from OCSF to ECS (datasource)
ECS_MAPPING = load_default_mapping("ecs")


def test_reverse_mapping_ipv4():
    reverse_map = reverse_mapping(STIX_MAPPING)
    ipv4 = reverse_map["ipv4-addr:value"]
    assert isinstance(ipv4, list)
    assert set(ipv4) == {"device.ip", "endpoint.ip"}


def test_reverse_mapping_executable():
    reverse_map = reverse_mapping(ECS_MAPPING)
    exe = reverse_map["process.executable"]
    assert isinstance(exe, list)
    assert "process.file.path" in exe
    for item in exe:
        if isinstance(item, dict):
            assert "ocsf_field" in item
            if item["ocsf_field"] == "process.file.name":
                # Make sure all metadata from the mapping got reversed
                assert item["native_value"] in ("posixpath_endswith", "winpath_endswith")
                assert item["native_op"] == "LIKE"
                assert item["ocsf_value"] == "basename"


@pytest.mark.parametrize(
    "dmm, field, op, value, expected_result",
    [
        (WINLOGBEAT_MAPPING, "process.file.path", "=", "C:\\TMP\\foo.exe",
         [("winlog.event_data.Image", "=", "C:\\TMP\\foo.exe")]),
        (WINLOGBEAT_MAPPING, "process.file.name", "=", "foo.exe",
         [("winlog.event_data.Image", "LIKE", "%\\foo.exe"),
          ("winlog.event_data.Image", "LIKE", "%/foo.exe")]),
        (ECS_MAPPING, "process.file.path", "=", "C:\\TMP\\foo.exe",
         [("process.executable", "=", "C:\\TMP\\foo.exe")]),
        (ECS_MAPPING, "process.file.name", "=", "foo.exe",
         [("process.executable", "LIKE", "%\\foo.exe"),
          ("process.executable", "LIKE", "%/foo.exe")]),
    ],
)
def test_translate_comparison_to_native(dmm, field, op, value, expected_result):
    assert translate_comparison_to_native(dmm, field, op, value) == expected_result


@pytest.mark.parametrize(
    "dmm, field, op, value, expected_result",
    [
        (ECS_MAPPING, "process.executable", "=", "C:\\TMP\\foo.exe",
         [
            ("process.file.path", "=", "C:\\TMP\\foo.exe"),
            ("process.file.name", "=", "foo.exe"),
            ("process.file.parent_folder", "=", "C:\\TMP"),
         ]),
        (ECS_MAPPING, "process.executable", "LIKE", "%\\foo.exe",
         [
            ("process.file.path", "LIKE", "%\\foo.exe"),
            ("process.file.name", "LIKE", "foo.exe"),     #TODO: could optimize this to "="
            ("process.file.parent_folder", "LIKE", "%"),  #TODO: could eliminate this?
         ]),
        (STIX_MAPPING, "ipv4-addr:value", "=", "198.51.100.13",
         [
             ("device.ip", "=", "198.51.100.13"),
             ("endpoint.ip", "=", "198.51.100.13"),
         ]),
    ],
)
def test_translate_comparison_to_ocsf(dmm, field, op, value, expected_result):
    """Test the translate function."""
    reverse_dmm = reverse_mapping(dmm)  # Make the dmms fixtures?
    assert set(translate_comparison_to_ocsf(reverse_dmm, field, op, value)) == set(expected_result)


@pytest.mark.parametrize(
    "dmm, entity, field, expected_result",
    [
        (WINLOGBEAT_MAPPING, "process", ["file.name", "pid"],
         [("winlog.event_data.Image", "file.name"), ("winlog.event_data.ProcessId", "pid")]),
        (WINLOGBEAT_MAPPING, "process", None,
         [("winlog.event_data.CommandLine", "cmd_line"),
          ("winlog.event_data.ProcessId", "pid"),
          ("winlog.event_data.ProcessGuid", "uid"),
          ("winlog.event_data.Image", "file.path"),
          ("winlog.event_data.Image", "file.name"),
          ("winlog.event_data.Image", "file.parent_folder"),
          ("winlog.event_data.ParentCommandLine", "parent_process.cmd_line"),
          ("winlog.event_data.ParentProcessId", "parent_process.pid"),
          ("winlog.event_data.ParentProcessGuid", "parent_process.uid"),
          ("winlog.event_data.ParentImage", "parent_process.file.path"),
          ("winlog.event_data.ParentImage", "parent_process.file.name"),
          ("winlog.event_data.ParentImage", "parent_process.file.parent_folder"),
         ]),
    ],
)
def test_translate_projection_to_native(dmm, entity, field, expected_result):
    assert translate_projection_to_native(dmm, entity, field) == expected_result


def test_translate_dataframe():  #TODO: more testing here
    df = pd.DataFrame({"file.path": [r"C:\Windows\System32\cmd.exe", r"C:\TMP"],
                       "pid": [1, 2]})
    dmm = load_default_mapping("ecs")
    df = translate_dataframe(df, dmm["process"])
    #TODO:assert df["file.name"].iloc[0] == "cmd.exe"


def test_incomplete_mapping_no_identifier():
    identifier_config = load_kestrel_config()["entity_identifier"]
    with pytest.raises(IncompleteDataMapping):
        check_entity_identifier_existence_in_mapping(INCOMPLETE_MAPPING, identifier_config, "test interface")
