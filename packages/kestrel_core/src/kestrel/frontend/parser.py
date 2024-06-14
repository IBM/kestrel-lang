# parse Kestrel syntax, apply frontend mapping, transform to IR

import os
import logging
from itertools import chain

from kestrel.frontend.compile import _KestrelT
from kestrel.mapping.data_model import reverse_mapping
from kestrel.utils import load_data_file
from kestrel.config.utils import list_yaml_files_in_module
from lark import Lark
from typeguard import typechecked
import yaml


_logger = logging.getLogger(__name__)


frontend_mapping = {}


@typechecked
def get_frontend_mapping(mapping_type: str, mapping_package: str) -> dict:
    global frontend_mapping
    mapping = frontend_mapping.get(mapping_type)
    if mapping is not None:
        return mapping
    for yaml_file in list_yaml_files_in_module(mapping_package):
        try:
            mapping_str = load_data_file(mapping_package, "ecs.yaml")
            mapping = yaml.safe_load(mapping_str)
            if mapping_type == "property":
                # New data model map is always OCSF->native
                mapping = reverse_mapping(mapping)
            frontend_mapping[mapping_type] = mapping
        except Exception as ex:
            _logger.error("Failed to load %s", mapping_str, exc_info=ex)
            mapping = None  # FIXME: this is not a dict
    return mapping


@typechecked
def get_keywords():
    # TODO: this Kestrel1 code needs to be updated
    grammar = load_data_file("kestrel.frontend", "kestrel.lark")
    parser = Lark(grammar, parser="lalr")
    alphabet_patterns = filter(lambda x: x.pattern.value.isalnum(), parser.terminals)
    # keywords = [x.pattern.value for x in alphabet_patterns] + all_relations
    keywords = [x.pattern.value for x in alphabet_patterns]
    keywords_lower = map(lambda x: x.lower(), keywords)
    keywords_upper = map(lambda x: x.upper(), keywords)
    keywords_comprehensive = list(chain(keywords_lower, keywords_upper))
    return keywords_comprehensive


# Create a single, reusable transformer
_parser = Lark(
    load_data_file("kestrel.frontend", "kestrel.lark"),
    parser="lalr",
    transformer=_KestrelT(
        entity_map=get_frontend_mapping(
            "entity", "kestrel.mapping.entityname"
        ),
        property_map=get_frontend_mapping(
            "property", "kestrel.mapping.entityattribute"
        ),
    ),
)


def parse_kestrel(stmts):
    return _parser.parse(stmts)
