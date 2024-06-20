import logging
from collections import OrderedDict
from functools import reduce
from typing import Optional, Union

import dpath
import numpy as np
import yaml
from pandas import DataFrame
from typeguard import typechecked

from kestrel.mapping.transformers import (
    run_transformer,
    run_transformer_on_series,
)
from kestrel.utils import list_folder_files
from kestrel.exceptions import IncompleteDataMapping

_logger = logging.getLogger(__name__)


def _add_mapping(obj: dict, key: str, mapping: dict):
    """Add `key` -> `mapping` to `obj`, appending if necessary"""
    existing_mapping = obj.get(key)
    if existing_mapping:
        if isinstance(existing_mapping, str):
            existing_mapping = [{"ocsf_field": existing_mapping}]
        elif isinstance(existing_mapping, dict):
            existing_mapping = [existing_mapping]
    else:
        existing_mapping = []
    existing_mapping.append(mapping)
    obj[key] = existing_mapping


def _reverse_dict(obj: dict, k: str, v: dict):
    """Reverse a single OCSF -> native mapping and add it to `obj`"""
    key = v["native_field"]
    mapping = {i: j for i, j in v.items() if i != "native_field"}
    mapping["ocsf_field"] = k
    _add_mapping(obj, key, mapping)


def _add_attr(obj: dict, key: str, value: str):
    """Add `key` -> `value` to `obj`, appending if necessary"""
    if key not in obj:
        obj[key] = value
    else:
        existing = obj[key]
        if isinstance(existing, str) and existing != value:
            obj[key] = [existing, value]
        elif value not in existing:
            existing.append(value)


def reverse_mapping(obj: dict, prefix: str = None, result: dict = None) -> dict:
    """Reverse the mapping of `obj`

    Newly loaded mapping from disk is OCSF -> native mapping. This function
    takes in such mapping, and reverse it to native -> OCSF mapping, which can
    be used by the frontend. The result mapping is flattened.

    To call the function: `reverse_mapping(ocsf_to_native_mapping)`

    Parameters:
        obj: mapping loaded from disk (OCSF -> native)
        prefix: key path to `obj`; used by the recursive function itself
        result: intermediate result mapping; used by the recursive function itself

    Returns:
        native -> OCSF mapping
    """
    if result is None:
        result = {}
    for k, v in obj.items():
        k = ".".join((prefix, k)) if prefix else k
        # Recurse if necessary
        if isinstance(v, str):
            _add_attr(result, v, k)
        elif isinstance(v, list):
            # Need to handle multiple mappings
            for i in v:
                if isinstance(i, str):
                    _add_attr(result, i, k)
                elif "native_field" in i:
                    _reverse_dict(result, k, i)
                else:
                    # Need to "deep" merge with current results
                    reverse_mapping(i, k, result)
        elif isinstance(v, dict):
            # First determine if this is a complex mapping or just another level
            if "native_field" in v:
                _reverse_dict(result, k, v)
            else:
                # Need to "deep" merge with current results
                reverse_mapping(v, k, result)

    return result


def _get_map_triple(d: dict, prefix: str, op: str, value) -> tuple:
    mapped_op = d.get(f"{prefix}_op")
    transform = d.get(f"{prefix}_value")
    new_value = run_transformer(transform, value)
    new_op = mapped_op if mapped_op else op
    return (d[f"{prefix}_field"], new_op, new_value)


def translate_comparison_to_native(
    dmm: dict, field: str, op: str, value: Union[str, int, float]
) -> list:
    """Translate the (`field`, `op`, `value`) triple using data model map `dmm`

    This function may be used in datasource interfaces to translate a comparison
    in the OCSF data model to the native data model, according to the data model
    mapping in `dmm`.

    This function translates the (`field`, `op`, `value`) triple into a list of
    translated triples based on the provided data model map. The data model map
    is a dictionary that maps fields from one data model to another. For
    example, if you have a field named "user.name" in your data model, but the
    corresponding field in the native data model is "username", then you can use
    the data model map to translate the field name.

    Parameters:
        dmm: A dictionary that maps fields from one data model to another.
        field: The field name to be translated.
        op: The comparison operator.
        value: The value to be compared against.

    Returns:
        A list of translated triples.

    Raises:
        KeyError: If the field cannot be found in the data model map.
    """
    _logger.debug("comp_to_native: %s %s %s", field, op, value)
    result = []
    mapping = dmm.get(field)
    if mapping:
        if isinstance(mapping, str):
            # Simple 1:1 field name mapping
            result.append((mapping, op, value))
        else:
            raise NotImplementedError("complex native mapping")
    else:
        try:
            node = reduce(dict.__getitem__, field.split("."), dmm)
            if isinstance(node, list):
                for i in node:
                    if isinstance(i, dict):
                        result.append(_get_map_triple(i, "native", op, value))
                    else:
                        result.append((i, op, value))
            elif isinstance(node, dict):
                result.append(_get_map_triple(node, "native", op, value))
            elif isinstance(node, str):
                result.append((node, op, value))
        except KeyError:
            # Pass-through
            result.append((field, op, value))
    _logger.debug("comp_to_native: return %s", result)
    return result


def translate_comparison_to_ocsf(
    dmm: dict, field: str, op: str, value: Union[str, int, float]
) -> list:
    """Translate the (`field`, `op`, `value`) triple using data model map `dmm`

    This function is used in the frontend to translate a comparison in
    the STIX (or, in the future, ECS) data model to the OCSF data
    model, according to the data model mapping in `dmm`.

    This function translates the (`field`, `op`, `value`) triple into a list of
    translated triples based on the provided data model map. The data model map
    is a dictionary that maps fields from one data model to another. For
    example, if you have a field named "user.name" in your data model, but the
    corresponding field in the native data model is "username", then you can use
    the data model map to translate the field name.

    Parameters:
        dmm: A dictionary that maps fields from one data model to another.
        field: The field name to be translated.
        op: The comparison operator.
        value: The value to be compared against.

    Returns:
        A list of translated triples.

    Raises:
        KeyError: If the field cannot be found in the data model map.

    """
    _logger.debug("comp_to_ocsf: %s %s %s", field, op, value)
    result = []
    mapping = dmm.get(field)
    if isinstance(mapping, str):
        # Simple 1:1 field name mapping
        result.append((mapping, op, value))
    elif isinstance(mapping, list):
        for i in mapping:
            if isinstance(i, dict):
                result.append(_get_map_triple(i, "ocsf", op, value))
            else:
                result.append((i, op, value))
    return result


@typechecked
def load_default_mapping(
    data_model_name: str,
    mapping_pkg: str = "kestrel.mapping",
    submodule: str = "entityattribute",
):
    result = {}
    for f in list_folder_files(
        mapping_pkg, submodule, prefix=data_model_name, extension="yaml"
    ):
        with open(f, "r") as fp:
            result.update(yaml.safe_load(fp))
    return result


@typechecked
def check_entity_identifier_existence_in_mapping(
    data_model_mapping: dict,
    entity_identifiers: dict,
    interface_information: Optional[str] = None,
):
    for entity_name, ids in entity_identifiers.items():
        if entity_name in data_model_mapping:
            entity = data_model_mapping[entity_name]
            for idx in ids:
                try:
                    reduce(dict.__getitem__, idx.split("."), entity)
                except KeyError:
                    msg_body = f"Identifier '{idx}' for entity '{entity_name}' is missing in data mapping"
                    appendix = (
                        f" at '{interface_information}'"
                        if interface_information
                        else ""
                    )
                    raise IncompleteDataMapping(msg_body + appendix)


@typechecked
def _get_from_mapping(mapping: Union[str, list, dict], key) -> list:
    result = []
    if isinstance(mapping, list):
        for i in mapping:
            if isinstance(i, dict):
                result.append(i[key])
            else:
                result.append(i)
    elif isinstance(mapping, dict):
        result.append(mapping[key])
    elif isinstance(mapping, str):
        result.append(mapping)
    return result


@typechecked
def translate_projection_to_native(
    dmm: dict,
    entity_type: Optional[str],
    attrs: Optional[list],
    # TODO: optional str or callable for joining entity_type and attr?
) -> list:
    result = []

    if entity_type:
        dmm = dmm[entity_type]

    if attrs:
        # project specified attributes
        for attr in attrs:
            try:
                mapping = reduce(dict.__getitem__, attr.split("."), dmm)
                result.extend(
                    [(i, attr) for i in _get_from_mapping(mapping, "native_field")]
                )
            except KeyError:
                # TODO: think better way than pass-through, e.g., raise exception
                _logger.warning(f"mapping not found for entity: '{entity_type}' and attribute: '{attr}'; treat it as no mapping needed")
                result.append((attr, attr))
    else:
        # project all attributes known for the entity (or event if no entity specified)
        for native_field, mapping in reverse_mapping(dmm).items():
            result.extend(
                [(native_field, i) for i in _get_from_mapping(mapping, "ocsf_field")]
            )

    # De-duplicate list while maintaining order
    final_result = list(OrderedDict.fromkeys(result))
    _logger.debug("proj_to_native: return %s", final_result)

    return final_result


@typechecked
def translate_projection_to_ocsf(
    dmm: dict,
    native_type: Optional[str],
    entity_type: Optional[str],
    attrs: list,
) -> list:
    result = []
    for attr in attrs:
        mapping = dmm.get(attr)
        if not mapping and native_type:
            mapping = dmm.get(f"{native_type}:{attr}", attr)  # FIXME: only for STIX
        else:
            mapping = attr
        ocsf_name = _get_from_mapping(mapping, "ocsf_field")
        if isinstance(ocsf_name, list):
            result.extend(ocsf_name)
        else:
            result.append(ocsf_name)
    if entity_type:
        # Need to prune the entity name
        prefix = f"{entity_type}."
        result = [
            field[len(prefix) :] if field.startswith(prefix) else field
            for field in result
        ]
    return result


@typechecked
def translate_dataframe(df: DataFrame, dmm: dict) -> DataFrame:
    # Translate results into Kestrel OCSF data model
    # The column names of df are already mapped
    for col in df.columns:
        try:
            mapping = dpath.get(dmm, col, separator=".")
        except KeyError:
            _logger.debug("No mapping for %s", col)
            mapping = None
        if isinstance(mapping, dict):
            transformer_name = mapping.get("ocsf_value")
            df[col] = run_transformer_on_series(transformer_name, df[col].dropna())
    df = df.replace({np.nan: None})
    return df
