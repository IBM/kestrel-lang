import logging
from typing import Any, Iterable, List, Mapping, MutableMapping, Optional, Tuple
from uuid import UUID

from kestrel.display import GraphletExplanation
from kestrel.exceptions import DataSourceError, InvalidDataSource
from kestrel.interface import DatasourceInterface
from kestrel.ir.graph import IRGraphEvaluable
from kestrel.ir.instructions import (
    DataSource,
    Filter,
    Instruction,
    Return,
    SolePredecessorTransformingInstruction,
    SourceInstruction,
    TransformingInstruction,
    Variable,
)
from kestrel.mapping.data_model import translate_dataframe
from kestrel_interface_opensearch.config import load_config
from kestrel_interface_opensearch.ossql import OpenSearchTranslator
from opensearchpy import OpenSearch
from pandas import DataFrame, concat

_logger = logging.getLogger(__name__)


def _jdbc2df(schema: dict, datarows: dict) -> DataFrame:
    """Convert a JDBC query result response to a DataFrame"""
    columns = [c.get("alias", c["name"]) for c in schema]
    return DataFrame(datarows, columns=columns)


def read_sql(sql: str, conn: OpenSearch, dmm: Optional[dict] = None) -> DataFrame:
    """Execute `sql` and return the results as a DataFrame, a la pandas.read_sql"""
    # https://opensearch.org/docs/latest/search-plugins/sql/sql-ppl-api/#query-api
    body = {
        # Temporarily comment out fetch_size due to https://github.com/opensearch-project/sql/issues/2579
        # FIXME: "fetch_size": 10000,  # Should we make this configurable?
        "query": sql,
    }
    query_resp = conn.http.post("/_plugins/_sql?format=jdbc", body=body)
    status = query_resp.get("status", 500)
    if status != 200:
        raise DataSourceError(f"OpenSearch query returned {status}")
    _logger.debug(
        "total=%d size=%d rows=%d",
        query_resp["total"],
        query_resp["size"],
        len(query_resp["datarows"]),
    )

    # Only the first page contains the schema
    # https://opensearch.org/docs/latest/search-plugins/sql/sql-ppl-api/#paginating-results
    schema = query_resp["schema"]
    dfs = []
    done = False
    while not done:
        df = _jdbc2df(schema, query_resp["datarows"])
        if dmm is not None:
            # Need to use Data Model Map to do results translation
            dfs.append(translate_dataframe(df, dmm))
        else:
            dfs.append(df)
        cursor = query_resp.get("cursor")
        if not cursor:
            break
        query_resp = conn.http.post(
            "/_plugins/_sql?format=jdbc", body={"cursor": cursor}
        )

    # Merge all pages together
    return concat(dfs)


class OpenSearchInterface(DatasourceInterface):
    def __init__(
        self,
        serialized_cache_catalog: Optional[str] = None,
        session_id: Optional[UUID] = None,
    ):
        super().__init__(serialized_cache_catalog, session_id)
        self.config = load_config()
        self.schemas: dict = {}  # Schema per table (index)
        self.conns: dict = {}  # Map of conn name -> connection
        for info in self.config.datasources.values():
            name = info.connection
            if name not in self.conns:
                conn = self.config.connections[name]
                client = OpenSearch(
                    [conn.url],
                    http_auth=(conn.auth.username, conn.auth.password),
                    verify_certs=conn.verify_certs,
                )
                self.conns[name] = client

    @staticmethod
    def schemes() -> Iterable[str]:
        return ["opensearch"]

    def get_datasources(self) -> List[str]:
        return list(self.config.datasources)

    def get_storage_of_datasource(datasource: str) -> str:
        """Get the storage name of a given datasource"""
        if datasource not in self.config.datasources:
            raise InvalidDataSource(datasource)
        return self.config.datasources[datasource].connection

    def store(
        self,
        instruction_id: UUID,
        data: DataFrame,
    ):
        raise NotImplementedError("OpenSearchInterface.store")  # TEMP

    def evaluate_graph(
        self,
        graph: IRGraphEvaluable,
        cache: MutableMapping[UUID, Any],
        instructions_to_evaluate: Optional[Iterable[Instruction]] = None,
    ) -> Mapping[UUID, DataFrame]:
        mapping = {}
        if not instructions_to_evaluate:
            instructions_to_evaluate = graph.get_sink_nodes()
        for instruction in instructions_to_evaluate:
            translator, datasource = self._evaluate_instruction_in_graph(
                graph, instruction
            )
            # TODO: may catch error in case evaluation starts from incomplete SQL
            sql = translator.result()
            _logger.debug("SQL query generated: %s", sql)
            ds = self.config.datasources[datasource]
            conn = self.config.connections[ds.connection]
            client = OpenSearch(
                [conn.url],
                http_auth=(conn.auth.username, conn.auth.password),
                verify_certs=conn.verify_certs,
            )
            mapping[instruction.id] = read_sql(
                sql, client, translator.from_ocsf_map[translator.entity]
            )
            client.close()
        return mapping

    def explain_graph(
        self,
        graph: IRGraphEvaluable,
        instructions_to_explain: Optional[Iterable[Instruction]] = None,
    ) -> Mapping[UUID, GraphletExplanation]:
        mapping = {}
        if not instructions_to_explain:
            instructions_to_explain = graph.get_sink_nodes()
        for instruction in instructions_to_explain:
            translator = self._evaluate_instruction_in_graph(graph, instruction)
            dep_graph = graph.duplicate_dependent_subgraph_of_node(instruction)
            graph_dict = dep_graph.to_dict()
            query_stmt = translator.result()
            mapping[instruction.id] = GraphletExplanation(graph_dict, query_stmt)
        return mapping

    def _evaluate_instruction_in_graph(
        self,
        graph: IRGraphEvaluable,
        instruction: Instruction,
    ) -> Tuple[OpenSearchTranslator, str]:
        _logger.debug("instruction: %s", str(instruction))
        translator = None
        datasource = None
        if isinstance(instruction, TransformingInstruction):
            trunk, _r2n = graph.get_trunk_n_branches(instruction)
            translator, datasource = self._evaluate_instruction_in_graph(graph, trunk)

            if isinstance(instruction, SolePredecessorTransformingInstruction):
                if isinstance(instruction, Return):
                    pass
                elif isinstance(instruction, Variable):
                    pass
                else:
                    translator.add_instruction(instruction)

            elif isinstance(instruction, Filter):
                translator.add_instruction(instruction)

            else:
                raise NotImplementedError(f"Unknown instruction type: {instruction}")

        elif isinstance(instruction, SourceInstruction):
            if isinstance(instruction, DataSource):
                datasource = instruction.datasource
                ds = self.config.datasources[instruction.datasource]
                schema = self.get_schema(instruction.datasource)
                translator = OpenSearchTranslator(
                    ds.timestamp_format,
                    ds.timestamp,
                    ds.index_pattern,
                    ds.data_model_map,
                    schema,
                )
            else:
                raise NotImplementedError(f"Unhandled instruction type: {instruction}")

        return translator, datasource

    def _get_client_for_datasource(self, datasource: str) -> OpenSearch:
        ds = self.config.datasources[datasource]
        conn = ds.connection
        _logger.debug(
            "Fetching schema for %s from %s",
            datasource,
            self.config.connections[conn].url,
        )
        return self.conns[conn], ds.index_pattern

    def get_schema(self, datasource: str) -> dict:
        client, index = self._get_client_for_datasource(datasource)
        if index not in self.schemas:
            df = read_sql(f"DESCRIBE TABLES LIKE {index}", client)
            self.schemas[datasource] = (
                df[["TYPE_NAME", "COLUMN_NAME"]]
                .set_index("COLUMN_NAME")
                .T.to_dict("records")[0]
            )
            _logger.debug("%s schema:\n%s", datasource, self.schemas[datasource])
        return self.schemas[datasource]
