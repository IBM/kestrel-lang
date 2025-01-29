import logging
import asyncio
import copy
from typing import Union
from typeguard import typechecked
from pandas import DataFrame
from multiprocessing import Queue

from kestrel.datasource import ReturnFromStore
from kestrel.utils import mkdtemp
from kestrel.exceptions import DataSourceError, DataSourceManagerInternalError
from kestrel_datasource_stixshifter.connector import setup_connector_module
from kestrel_datasource_stixshifter import multiproc
from kestrel_datasource_stixshifter.worker import STOP_SIGN
from kestrel_datasource_stixshifter.async_transmitter import AsyncTransmitter
from kestrel_datasource_stixshifter.subquery import split_subquery_by_time_window
from kestrel_datasource_stixshifter.async_translator import AsyncTranslator
from kestrel_datasource_stixshifter.config import (
    get_datasource_from_profiles,
    load_options,
    load_profiles,
    set_stixshifter_logging_level,
)

from firepit.sqlstorage import SqlStorage
import firepit.aio.ingest
import firepit.aio.asyncwrapper

from stix_shifter.stix_translation import stix_translation

# TODO: better solution to avoid using nest_asyncio for run_until_complete()
#       maybe putting entire Kestrel in async mode
import nest_asyncio

nest_asyncio.apply()


_logger = logging.getLogger(__name__)


def query_datasource(uri, pattern, session_id, config, store, limit=None):
    # CONFIG command is not supported
    # profiles will be updated according to YAML file and env var
    config["profiles"] = load_profiles()

    config["options"] = load_options()

    _logger.debug(
        "fast_translate enabled for: " f"{config['options']['fast_translate']}"
    )

    scheme, _, profile = uri.rpartition("://")
    profiles = profile.split(",")
    if scheme != "stixshifter":
        raise DataSourceManagerInternalError(
            f"interface {__package__} should not process scheme {scheme}"
        )

    set_stixshifter_logging_level()

    # cache_dir will be empty if not in debug mode
    # however, the directory guarantees unique and non-exist query ID
    # so it always needs to be created
    cache_dir = mkdtemp()
    query_id = cache_dir.name

    _logger.debug(f"prepare query with ID: {query_id}")

    num_records = 0
    limit_per_profile = limit

    for profile in profiles:
        if limit:
            if num_records >= limit:
                break
            if num_records > 0:
                limit_per_profile = limit - num_records
        _logger.debug(f"entering stix-shifter data source: {profile}")
        _logger.debug(f"profile = {profile}, limit_per_profile = {limit_per_profile}")
        # STIX-shifter will alter the config objects, thus making them not reusable.
        # So only give STIX-shifter a copy of the configs.
        # Check `modernize` functions in the `stix_shifter_utils` for details.
        (
            connector_name,
            connection_dict,
            configuration_dict,
            retrieval_batch_size,
            cool_down_after_transmission,
            allow_dev_connector,
            verify_cert,
            subquery_time_window,
        ) = map(
            copy.deepcopy, get_datasource_from_profiles(profile, config["profiles"])
        )

        setup_connector_module(connector_name, allow_dev_connector)

        if _logger.isEnabledFor(logging.DEBUG):
            data_path_striped = "".join(filter(str.isalnum, profile))
            cache_data_path_prefix = str(cache_dir / data_path_striped)
        else:
            cache_data_path_prefix = None

        observation_metadata = gen_observation_metadata(connector_name, query_id)

        for pattern in split_subquery_by_time_window(pattern, subquery_time_window):

            if limit_per_profile:
                if num_records >= limit_per_profile:
                    _logger.debug("do not execute subquery due to limit return reached")
                    break
                if num_records > 0:
                    limit_per_profile = limit_per_profile - num_records

            dsl = translate_query(
                connector_name, observation_metadata, pattern, connection_dict
            )

            raw_records_queue = Queue()
            translated_data_queue = Queue()

            exceptions = []

            with multiproc.translate(
                connector_name,
                observation_metadata,
                connection_dict.get("options", {}),
                cache_data_path_prefix,
                connector_name in config["options"]["fast_translate"],
                raw_records_queue,
                translated_data_queue,
                config["options"]["translation_workers_count"],
            ):
                with multiproc.transmit(
                    connector_name,
                    connection_dict,
                    configuration_dict,
                    retrieval_batch_size,
                    config["options"]["translation_workers_count"],
                    cool_down_after_transmission,
                    verify_cert,
                    dsl["queries"],
                    raw_records_queue,
                    limit_per_profile,
                ):
                    for result in multiproc.read_translated_results(
                        translated_data_queue,
                        config["options"]["translation_workers_count"],
                    ):
                        num_records += get_num_objects(result)
                        ingest(result, observation_metadata, query_id, store)

    return ReturnFromStore(query_id)


@typechecked
async def query_datasource_async(uri, pattern, session_id, config, store, limit=None, custom_mappings=None):
    config["profiles"] = load_profiles()
    config["options"] = load_options()

    _logger.debug(
        "fast_translate enabled for: " f"{config['options']['fast_translate']}"
    )

    scheme, _, profile = uri.rpartition("://")
    profiles = profile.split(",")
    if scheme != "stixshifter":
        raise DataSourceManagerInternalError(
            f"interface {__package__} should not process scheme {scheme}"
        )

    set_stixshifter_logging_level()

    cache_dir = mkdtemp()
    query_id = cache_dir.name

    _logger.debug(f"prepare query with ID: {query_id}")

    num_records = 0
    limit_per_profile = limit

    for profile in profiles:
        if limit:
            if num_records >= limit:
                break
            if num_records > 0:
                limit_per_profile = limit - num_records
        _logger.debug(f"entering stix-shifter data source: {profile}")
        _logger.debug(f"profile = {profile}, limit_per_profile = {limit_per_profile}")

        (
            connector_name,
            connection_dict,
            configuration_dict,
            retrieval_batch_size,
            cool_down_after_transmission,
            allow_dev_connector,
            verify_cert,
            subquery_time_window,
        ) = map(
            copy.deepcopy, get_datasource_from_profiles(profile, config["profiles"])
        )

        setup_connector_module(connector_name, allow_dev_connector)

        if _logger.isEnabledFor(logging.DEBUG):
            data_path_striped = "".join(filter(str.isalnum, profile))
            cache_data_path_prefix = str(cache_dir / data_path_striped)
        else:
            cache_data_path_prefix = None

        observation_metadata = gen_observation_metadata(connector_name, query_id)

        for sub_pattern in split_subquery_by_time_window(pattern, subquery_time_window):

            if limit_per_profile:
                if num_records >= limit_per_profile:
                    _logger.debug("do not execute subquery due to limit return reached")
                    break
                if num_records > 0:
                    limit_per_profile = limit_per_profile - num_records

            dsl = translate_query(
                connector_name, observation_metadata, sub_pattern, connection_dict, custom_mappings
            )

            raw_records_queue = Queue()
            translated_data_queue = Queue()

            
                # Start transmitters asynchronously
            transmitters = [
                    AsyncTransmitter(
                        connector_name,
                        connection_dict,
                        configuration_dict,
                        retrieval_batch_size,
                        query,
                        raw_records_queue,
                        limit_per_profile,
                        cool_down_after_transmission,
                        verify_cert,
                        custom_mappings
                    )
                    for query in dsl["queries"]
                ]
            transmitter_tasks = [transmitter.run() for transmitter in transmitters]
            await asyncio.gather(*transmitter_tasks)

                # Signal translators to stop
            await put_in_queue(raw_records_queue, STOP_SIGN)
            
            async_translator = AsyncTranslator(
                connector_name,
                observation_metadata,
                connection_dict.get("options", {}),
                cache_data_path_prefix,
                connector_name in config["options"]["fast_translate"],
                raw_records_queue,
                translated_data_queue,
                custom_mappings,
            )

            await async_translator.run_async()
                # Read translated results
            for result in async_translator.read_translated_results(
                translated_data_queue,
            ):
                num_records += get_num_objects(result)
                ingest(result, observation_metadata, query_id, store)

    return ReturnFromStore(query_id)


async def put_in_queue(queue: Queue, packet):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, queue.put, packet)


@typechecked
def gen_observation_metadata(connector_name: str, query_id: str):
    return {
        "id": "identity--" + query_id,
        "name": connector_name,
        "type": "identity",
    }


@typechecked
def translate_query(
    connector_name: str,
    observation_metadata: dict,
    pattern: str,
    connection_dict: dict,
    custom_mappings = None,
):
    translation = stix_translation.StixTranslation()
    translation_options = copy.deepcopy(connection_dict.get("options", {}))

    _logger.debug(f"STIX pattern to query: {pattern}")

    dsl = translation.translate(
        connector_name, "query", observation_metadata, pattern, translation_options, custom_mapping=custom_mappings
    )

    _logger.debug(f"translate results: {dsl}")

    if "error" in dsl:
        raise DataSourceError(
            "STIX-shifter translation from STIX"
            " to native query failed"
            f" with message: {dsl['error']}"
        )

    return dsl


@typechecked
def ingest(
    result: Union[dict, DataFrame],
    observation_metadata: dict,
    query_id: str,
    store: SqlStorage,
):
    _logger.debug("ingestion of a batch/page starts")
    if isinstance(result, DataFrame):
        # fast translation result in DataFrame
        asyncio.run(
            firepit.aio.ingest.ingest(
                firepit.aio.asyncwrapper.SyncWrapper(store=store),
                observation_metadata,
                result,
                query_id,
            )
        )
    else:
        # STIX bundle (normal stix-shifter translation result)
        store.cache(query_id, result)
    _logger.debug("ingestion of a batch/page ends")


@typechecked
def get_num_objects(data: Union[dict, DataFrame]):
    if isinstance(data, DataFrame):
        num_objects = len(data)
    else:
        num_objects = len(data.get("objects", []))
        if num_objects > 0:
            num_objects -= 1  # minus the identify object
    return num_objects
