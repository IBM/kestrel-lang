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
from kestrel_datasource_stixshifter.worker.utils import TranslationResult, WorkerLog


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

                    mapping = translation.translate_async(
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
                                    self.output_queue.put(packet_extra)

                else:
                    stixbundle = translation.translate(
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
                            self.output_queue.put(packet_extra)

            else:  # rely transmission error/info/debug message
                packet = input_batch

            self.output_queue.put(packet)

        self.output_queue.put(STOP_SIGN)

    def get_cache_data_path(self, offset, suffix):
        offset = str(offset).zfill(32)
        return f"{self.cache_data_path_prefix}_{offset}.{suffix}"