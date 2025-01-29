import asyncio
import json
import logging
from typing import Optional
from multiprocessing import Process, Queue, current_process
from typeguard import typechecked

from stix_shifter.stix_translation import stix_translation
from stix_shifter_utils.stix_translation.src.utils.transformer_utils import (
    get_module_transformers,
)
import firepit.aio.ingest

from kestrel_datasource_stixshifter.worker import STOP_SIGN
from kestrel.exceptions import DataSourceError
from kestrel_datasource_stixshifter.worker.utils import TranslationResult, WorkerLog


_logger = logging.getLogger(__name__)
@typechecked
class AsyncTranslator:
    def __init__(
        self,
        connector_name: str,
        observation_metadata: dict,
        translation_options: dict,
        cache_data_path_prefix: Optional[str],
        is_fast_translation: bool,
        input_queue: Queue,
        output_queue: Queue,
        custom_mappings = None,
    ):
        self.connector_name = connector_name
        self.observation_metadata = observation_metadata
        self.translation_options = translation_options
        self.cache_data_path_prefix = cache_data_path_prefix
        self.is_fast_translation = is_fast_translation
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.custom_mappings = custom_mappings

    async def run_async(self):
        worker_name = current_process().name
        translation = stix_translation.StixTranslation()

        for input_batch in iter(self.input_queue.get, STOP_SIGN):
            if input_batch.success:
                if self.is_fast_translation:
                    transformers = get_module_transformers(self.connector_name)

                    mapping = await translation.translate_async(
                        self.connector_name,
                        stix_translation.MAPPING,
                        None,
                        None,
                        self.translation_options,
                        custom_mapping=self.custom_mappings,
                    )

                    if "error" in mapping:
                        packet = TranslationResult(
                            worker_name,
                            False,
                            None,
                            WorkerLog(
                                logging.ERROR,
                                f"STIX-shifter mapping failed: {mapping['error']}",
                            ),
                        )
                    else:
                        try:
                            dataframe = firepit.aio.ingest.translate(
                                mapping["to_stix_map"],
                                transformers,
                                input_batch.data,
                                self.observation_metadata,
                            )
                        except Exception as e:
                            packet = TranslationResult(
                                worker_name,
                                False,
                                None,
                                WorkerLog(
                                    logging.ERROR,
                                    f"firepit.aio.ingest.translate() failed with msg: {str(e)}",
                                ),
                            )
                        else:
                            packet = TranslationResult(
                                worker_name,
                                True,
                                dataframe,
                                None,
                            )

                            if self.cache_data_path_prefix:
                                debug_df_filepath = self.get_cache_data_path(
                                    input_batch.offset,
                                    "parquet",
                                )
                                try:
                                    dataframe.to_parquet(debug_df_filepath)
                                except Exception as e:
                                    packet_extra = TranslationResult(
                                        worker_name,
                                        False,
                                        None,
                                        WorkerLog(
                                            logging.ERROR,
                                            f"STIX-shifter fast translation parquet write to disk failed: [{type(e).__name__}] {e}",
                                        ),
                                    )
                                    await put_in_queue(self.output_queue, packet_extra)

                else:
                    stixbundle = await translation.translate_async(
                        self.connector_name,
                        "results",
                        self.observation_metadata,
                        input_batch.data,
                        self.translation_options,
                        custom_mapping=self.custom_mappings,
                    )
                    if "error" in stixbundle:
                        packet = TranslationResult(
                            worker_name,
                            False,
                            None,
                            WorkerLog(
                                logging.ERROR,
                                f"STIX-shifter translation to STIX failed: {stixbundle['error']}",
                            ),
                        )
                    else:
                        packet = TranslationResult(
                            worker_name,
                            True,
                            stixbundle,
                            None,
                        )

                    if self.cache_data_path_prefix:
                        debug_stixbundle_filepath = self.get_cache_data_path(
                            input_batch.offset,
                            "json",
                        )
                        try:
                            with open(debug_stixbundle_filepath, "w") as bundle_fp:
                                json.dump(stixbundle, bundle_fp, indent=4)
                        except:
                            packet_extra = TranslationResult(
                                worker_name,
                                False,
                                None,
                                WorkerLog(
                                    logging.ERROR,
                                    f"STIX-shifter translation bundle write to disk failed",
                                ),
                            )
                            await put_in_queue(self.output_queue, packet_extra)

            else:  # rely transmission error/info/debug message
                packet = input_batch

            await put_in_queue(self.output_queue, packet)

        await put_in_queue(self.output_queue, STOP_SIGN)

    def get_cache_data_path(self, offset, suffix):
        offset = str(offset).zfill(32)
        return f"{self.cache_data_path_prefix}_{offset}.{suffix}"
    
    def read_translated_results(self,queue: Queue):
        for packet in iter(queue.get, STOP_SIGN):
            if packet.success:
                yield packet.data
            else:
                log_msg = f"[worker: {packet.worker}] {packet.log.log}"
                if packet.log.level == logging.ERROR:
                    _logger.debug(log_msg)
                    raise DataSourceError(log_msg)
                else:
                    if packet.log.level == logging.WARN:
                        _logger.warn(log_msg)
                    elif packet.log.level == logging.INFO:
                        _logger.info(log_msg)
                    else:  # all others as debug logs
                        _logger.debug(log_msg)
    
async def put_in_queue(queue: Queue, packet):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, queue.put, packet)