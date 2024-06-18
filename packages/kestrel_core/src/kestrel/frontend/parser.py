# parse Kestrel syntax, apply frontend mapping, transform to IR

import logging
from itertools import chain

from kestrel.frontend.compile import _KestrelT
from kestrel.mapping.data_model import reverse_mapping
from kestrel.utils import load_data_file, list_folder_files
from lark import Lark
from typeguard import typechecked
import yaml


_logger = logging.getLogger(__name__)


# cache mapping in the module
frontend_mapping = {}


@typechecked
def get_frontend_mapping(mapping_type: str, mapping_pkg: str, submodule: str) -> dict:
    global frontend_mapping
    if mapping_type not in frontend_mapping:
        mapping = {}
        for f in list_folder_files(mapping_pkg, submodule, extension="yaml"):
            with open(f, "r") as fp:
                mapping_ind = yaml.safe_load(fp)
            if mapping_type == "property":
                # New data model map is always OCSF->native
                mapping_ind = reverse_mapping(mapping_ind)
            # the entity mapping or reversed property mapping is flatten structure
            # so just dict.update() will do
            mapping.update(mapping_ind)
        frontend_mapping[mapping_type] = mapping
    return frontend_mapping[mapping_type]


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
        entity_map=get_frontend_mapping("entity", "kestrel.mapping", "entityname"),
        property_map=get_frontend_mapping(
            "property", "kestrel.mapping", "entityattribute"
        ),
    ),
)


def parse_kestrel(stmts):
    return _parser.parse(stmts)
