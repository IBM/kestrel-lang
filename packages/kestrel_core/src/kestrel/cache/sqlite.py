import logging
from copy import copy
from typing import Iterable, Mapping, Optional, Union, Any
from uuid import UUID

import sqlalchemy
from dateutil.parser import parse as dt_parser
from pandas import DataFrame, read_sql
from typeguard import typechecked

from kestrel.cache.base import AbstractCache
from kestrel.interface.codegen.sql import SqlTranslator
from kestrel.ir.graph import IRGraphEvaluable
from kestrel.display import GraphletExplanation, NativeQuery
from kestrel.ir.instructions import (
    Construct,
    Instruction,
    Return,
    Explain,
    Variable,
    Filter,
    SourceInstruction,
    TransformingInstruction,
    SolePredecessorTransformingInstruction,
)

_logger = logging.getLogger(__name__)


@typechecked
class SqliteTranslator(SqlTranslator):
    def __init__(self, from_obj: Union[sqlalchemy.sql.expression.CTE, str]):
        if isinstance(from_obj, sqlalchemy.sql.expression.CTE):
            fc = from_obj
        else:  # str to represent table name
            fc = sqlalchemy.table(from_obj)
        super().__init__(
            sqlalchemy.dialects.sqlite.dialect(), dt_parser, "time", fc
        )  # FIXME: need mapping for timestamp?
        self.associated_variable = None


@typechecked
class SqliteCache(AbstractCache):
    def __init__(
        self,
        initial_cache: Optional[Mapping[UUID, DataFrame]] = None,
        session_id: Optional[UUID] = None,
    ):
        super().__init__()

        basename = session_id or "cache"
        self.db_path = f"{basename}.db"

        # for an absolute file path, the three slashes are followed by the absolute path
        # for a relative path, it's also three slashes?
        self.engine = sqlalchemy.create_engine(f"sqlite:///{self.db_path}")
        self.connection = self.engine.connect()

        if initial_cache:
            for instruction_id, data in initial_cache.items():
                self[instruction_id] = data

    def __del__(self):
        self.connection.close()

    def __getitem__(self, instruction_id: UUID) -> DataFrame:
        return read_sql(self.cache_catalog[instruction_id], self.connection)

    def __delitem__(self, instruction_id: UUID):
        table_name = self.cache_catalog[instruction_id]
        self.connection.execute(sqlalchemy.text(f'DROP TABLE "{table_name}"'))
        del self.cache_catalog[instruction_id]

    def __setitem__(
        self,
        instruction_id: UUID,
        data: DataFrame,
    ):
        table_name = instruction_id.hex
        self.cache_catalog[instruction_id] = table_name
        data.to_sql(table_name, con=self.connection, if_exists="replace", index=False)

    def get_virtual_copy(self) -> AbstractCache:
        v = copy(self)
        v.cache_catalog = copy(self.cache_catalog)
        v.__class__ = SqliteCacheVirtual
        return v

    def evaluate_graph(
        self,
        graph: IRGraphEvaluable,
        instructions_to_evaluate: Optional[Iterable[Instruction]] = None,
    ) -> Mapping[UUID, DataFrame]:
        mapping = {}
        if not instructions_to_evaluate:
            instructions_to_evaluate = graph.get_sink_nodes()
        for instruction in instructions_to_evaluate:
            _logger.debug(f"evaluate instruction: {instruction}")
            translator = self._evaluate_instruction_in_graph(graph, instruction)
            # TODO: may catch error in case evaluation starts from incomplete SQL
            _logger.debug(f"SQL query generated: {translator.result_w_literal_binds()}")
            mapping[instruction.id] = read_sql(translator.result(), self.connection)
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
            dep_graph = graph.duplicate_dependent_subgraph_of_node(instruction)
            graph_dict = dep_graph.to_dict()
            translator = self._evaluate_instruction_in_graph(graph, instruction)
            query = NativeQuery("SQL", str(translator.result_w_literal_binds()))
            mapping[instruction.id] = GraphletExplanation(graph_dict, query)
        return mapping

    def _evaluate_instruction_in_graph(
        self,
        graph: IRGraphEvaluable,
        instruction: Instruction,
        cte_memory: Optional[Mapping[UUID, sqlalchemy.sql.expression.CTE]] = None,
    ) -> SqliteTranslator:
        """Evaluate the instruction in the graph

        This method recursively traverse the graph from the instruction node to
        evaluate the instruction with all its dependencies.

        To avoid repeated traversal/evaluation of the same subgraph/subtree,
        for each Variable instruction/node, the method performs dynamic
        programming in the form of memorization subgraph results as CTEs. This
        advanced feature requires the underlying SQL engine to support common
        table expression (CTE), which may not be possible for query engines
        like SQL on OpenSearch (Kestrel OpenSearch interface uses embedded
        subquery instead of CTE).

        To avoid unexpected Python behavior
        https://docs.quantifiedcode.com/python-anti-patterns/correctness/mutable_default_value_as_argument.html
        We use `None` as default value instead of `{}`

        Parameters:
            graph: the graph to traverse
            instruction: the instruction to evaluate/return
            cte_memory: memorize the subgraph traversed/evaluated in CTE

        Returns:
            A translator (SQL statements) to be executed
        """
        if cte_memory is None:
            cte_memory = {}

        if instruction.id in self:
            # cached in sqlite
            table_name = self.cache_catalog[instruction.id]
            translator = SqliteTranslator(table_name)

        elif isinstance(instruction, SourceInstruction):
            if isinstance(instruction, Construct):
                # cache the data
                self[instruction.id] = DataFrame(instruction.data)
                # pull the data to start a SqliteTranslator
                table_name = self.cache_catalog[instruction.id]
                translator = SqliteTranslator(table_name)
            else:
                raise NotImplementedError(f"Unknown instruction type: {instruction}")

        elif isinstance(instruction, TransformingInstruction):
            if instruction.id in cte_memory:
                # this is a Variable, already evaluated
                # just create a new use/translator from this Variable
                translator = SqliteTranslator(cte_memory[instruction.id])
            else:
                trunk, r2n = graph.get_trunk_n_branches(instruction)
                translator = self._evaluate_instruction_in_graph(
                    graph, trunk, cte_memory
                )

                if isinstance(instruction, SolePredecessorTransformingInstruction):
                    if isinstance(instruction, (Return, Explain)):
                        pass
                    elif isinstance(instruction, Variable):
                        cte = translator.query.cte(name=instruction.name)
                        cte_memory[instruction.id] = cte
                        translator = SqliteTranslator(cte)
                    else:
                        translator.add_instruction(instruction)

                elif isinstance(instruction, Filter):
                    # replace each ReferenceValue with a subquery
                    # note that this subquery will be used as a value for the .in_ operator
                    # we should not use .subquery() here but just `Select` class
                    # otherwise, will get warning:
                    #   SAWarning: Coercing Subquery object into a select() for use in IN();
                    #   please pass a select() construct explicitly
                    instruction.resolve_references(
                        lambda x: self._evaluate_instruction_in_graph(
                            graph, r2n[x], cte_memory
                        ).query
                    )
                    translator.add_instruction(instruction)

                else:
                    raise NotImplementedError(
                        f"Unknown instruction type: {instruction}"
                    )

        else:
            raise NotImplementedError(f"Unknown instruction type: {instruction}")

        return translator


@typechecked
class SqliteCacheVirtual(SqliteCache):
    def __getitem__(self, instruction_id: UUID) -> Any:
        return self.cache_catalog[instruction_id]

    def __delitem__(self, instruction_id: UUID):
        del self.cache_catalog[instruction_id]

    def __setitem__(self, instruction_id: UUID, data: Any):
        self.cache_catalog[instruction_id] = instruction_id.hex + "v"

    def __del__(self):
        pass
