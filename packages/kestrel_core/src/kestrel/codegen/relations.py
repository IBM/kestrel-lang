"""Relationship mapping from Kestrel relation to STIX references.

STIX reference names may not be the original STIX reference name. The names
used here are pre-processed by :func:`firepit.raft.invert`. Check the function
for more details.

"""

import datetime
from collections import defaultdict
import logging

from firepit.timestamp import to_datetime
from kestrel.syntax.reference import value_to_stix
from firepit.query import Column, Join, Query, Projection, Table, Unique

_logger = logging.getLogger(__name__)

stix_2_0_ref_mapping = {
    # (EntityX, Relate, EntityY): ([EntityX_STIX_Ref_i], [EntityY_STIX_Ref_i])
    # All STIX 2.0 refs enumerated
    # file
    ("file", "contained", "artifact"): (["content_ref"], []),
    ("directory", "contained", "directory"): (["contains_refs"], ["contains_refs"]),
    ("directory", "contained", "file"): (["contains_refs"], ["parent_directory_ref"]),
    ("archive-ext", "contained", "file"): (["contains_refs"], []),
    # email
    ("user-account", "owned", "email-addr"): ([], ["belongs_to_ref"]),
    ("email-addr", "created", "email-message"): ([], ["from_ref", "sender_ref"]),
    ("email-addr", "accepted", "email-message"): (
        [],
        ["to_refs", "cc_refs", "bcc_refs"],
    ),
    ("email-message", None, "artifact"): (["raw_email_ref", "body_raw_ref"], []),
    ("email-message", None, "file"): (
        ["body_raw_ref"],
        [],
    ),  # FIXME: should be mime-part-type?
    # ip address
    ("autonomous-system", "owned", "ipv4-addr"): ([], ["belongs_to_refs"]),
    ("autonomous-system", "owned", "ipv6-addr"): ([], ["belongs_to_refs"]),
    # network-traffic
    ("ipv4-addr", "created", "network-traffic"): ([], ["src_ref"]),
    ("ipv6-addr", "created", "network-traffic"): ([], ["src_ref"]),
    ("mac-addr", "created", "network-traffic"): ([], ["src_ref"]),
    ("domain-name", "created", "network-traffic"): ([], ["src_ref"]),
    ("artifact", "created", "network-traffic"): ([], ["src_payload_ref"]),
    ("mac-addr", None, "ipv4-addr"): ([], ["resolves_to_refs"]),
    ("mac-addr", None, "ipv6-addr"): ([], ["resolves_to_refs"]),
    ("http-request-ext", None, "artifact"): (["message_body_data_ref"], []),
    ("ipv4-addr", "accepted", "network-traffic"): ([], ["dst_ref"]),
    ("ipv6-addr", "accepted", "network-traffic"): ([], ["dst_ref"]),
    ("mac-addr", "accepted", "network-traffic"): ([], ["dst_ref"]),
    ("domain-name", "accepted", "network-traffic"): ([], ["dst_ref"]),
    ("artifact", "accepted", "network-traffic"): ([], ["dst_payload_ref"]),
    ("network-traffic", "contained", "network-traffic"): (
        ["encapsulated_by_ref"],
        ["encapsulated_by_ref"],
    ),
    # process
    ("process", "created", "network-traffic"): (["opened_connection_refs"], []),
    ("user-account", "owned", "process"): ([], ["creator_user_ref"]),
    ("process", "loaded", "file"): (["binary_ref"], []),
    # ("process", "created", "process"): (["child_refs"], ["parent_ref"]),
    ("process", "created", "process"): ([], ["parent_ref"]),
    # service
    ("windows-service-ext", "loaded", "file"): (["service_dll_refs"], []),
    ("windows-service-ext", "loaded", "user-account"): (["creator_user_ref"], []),
}

# the first available attribute will be used to uniquely identify the entity
stix_2_0_identical_mapping = {
    # entity-type: id attributes candidates
    "directory": ("path",),
    "domain-name": ("value",),
    "email-addr": ("value",),
    "file": (
        "hashes.'SHA-256'",
        "hashes.'SHA-1'",
        "hashes.MD5",
        "name",
    ),  # optional in STIX standard
    "ipv4-addr": ("value",),
    "ipv6-addr": ("value",),
    "mac-addr": ("value",),
    "mutex": ("name",),
    # `pid` is optional in STIX standard
    # `first_observed` cannot be used since it may be wrong (derived from observation)
    # `command_line` or `name` may not be in data and cannot be used
    "process": (
        "x_unique_id",
        "pid",
    ),
    "software": ("name",),
    "url": ("value",),
    "user-account": ("user_id",),  # optional in STIX standard
    "windows-registry-key": ("key",),  # optional in STIX standard
    "x-oca-asset": ("device_id",),  # oca/stix-extension repo
}

# no direction for generic relations
generic_relations = ["linked"]

all_relations = list(
    set([x[1] for x in stix_2_0_ref_mapping.keys() if x[1]] + generic_relations)
)


def get_entity_id_attribute(variable):
    # this function should always return something
    # if no entity id attribute found, fall back to record "id" by default
    # this works for:
    #   - no appriparite identifier attribute found given specific data
    #   - "network-traffic" (not in stix_2_0_identical_mapping)
    id_attr = "id"

    if variable.type in stix_2_0_identical_mapping:
        available_attributes = variable.store.columns(variable.entity_table)
        for attr in stix_2_0_identical_mapping[variable.type]:
            if attr in available_attributes:
                query = Query()
                query.append(Table(variable.entity_table))
                query.append(Projection([attr]))
                query.append(Unique())
                rows = variable.store.run_query(query).fetchall()
                all_values = [row[attr] for row in rows if row[attr]]
                if all_values:
                    id_attr = attr
                    break

    return id_attr


def compile_identical_entity_search_pattern(var_name, var_struct, does_support_id):
    # "id" attribute may not be available for STIX 2.0 via STIX-shifter
    # so `does_support_id` is set to False in default kestrel config file
    attribute = get_entity_id_attribute(var_struct)
    if attribute == "id" and not does_support_id:
        pattern_raw = None
    else:
        pattern_raw = f"[{var_struct.type}:{attribute} = {var_name}.{attribute}]"
    _logger.debug(f"identical entity search raw pattern generated: {pattern_raw}")
    return pattern_raw


def fine_grained_relational_process_filtering(
    local_var, prefetch_entity_table, store, config
):
    # Two-step search for matched processes
    # 1. anchor process search (find anchor process in pfeh_processes against ref_processes)
    # 2. precise process search (find process in pfeh_processes against anchor_processes)

    # two situations worth mentioning:
    # - in Linux, a new process will be forked, then exec to change name. In this case,
    #   we need to search for anchor_rows to identify process with even name changed,
    #   then get all records of both process before name change and after name change.
    # - ppid is useful to identify a process with pid, however, in one situation, the
    #   ppid data is not available in the first phase of FIND (creating of local_var
    #   using deref in firepit)---FIND parent process of current process. This is because
    #   most datasource does not store *parent parent process pid* for deref to get ppid
    #   of the parent. In this case, we need to search for anchor_rows to infer the ppid.

    _logger.debug(
        f"start fine-grained relational process filtering for prefetched table: {prefetch_entity_table}"
    )

    # reference processes obtained from de-referring data in firepit
    # type(ref_processes) == {pid: (rid, (pname, ppid, start_time, end_time))}
    ref_processes = _query_process_with_time_and_ppid(store, local_var.entity_table)

    # prefetched processes to be filtered
    # type(pfeh_processes) == {pid: (rid, (pname, ppid, start_time, end_time))}
    pfeh_processes = _query_process_with_time_and_ppid(store, prefetch_entity_table)

    # 1. anchor process search (a subset of pfeh_processes that matches ref_processes)
    # type(anchor_processes) == {pid: (rid, (pname, ppid, start_time, end_time))}
    anchor_processes = _search_for_potential_identical_process(
        ref_processes, pfeh_processes, config
    )

    anchor_proc_cnt = sum(map(len, anchor_processes.values()))
    prefetched_proc_cnt = sum(map(len, pfeh_processes.values()))
    _logger.debug(
        f"found {anchor_proc_cnt} anchor rows out of {prefetched_proc_cnt} raw prefetched."
    )

    # 2. precise process search (a larger subset of pfeh_processes that matches anchor_processes)
    # type(anchor_processes) == {pid: (rid, (pname, ppid, start_time, end_time))}
    filtered_processes = _search_for_potential_identical_process(
        anchor_processes, pfeh_processes, config
    )

    filtered_ids = [rid for procs in filtered_processes.values() for rid, _ in procs]
    filtered_ids = list(set(filtered_ids))

    _logger.debug(
        f"found {len(filtered_ids)} out of {prefetched_proc_cnt} raw prefetched results to be true relational process records."
    )

    return filtered_ids


def build_pattern_from_ids(return_type, ids):
    if ids:
        return (
            "[" + return_type + ":id IN (" + ", ".join(map(value_to_stix, ids)) + ")]"
        )
    else:
        return None


def _query_process_with_time_and_ppid(store, var_table_name):
    pid2procs = defaultdict(list)

    if "parent_ref" in store.columns(var_table_name):
        has_parent_ref = True
    else:
        has_parent_ref = False

    query_details = [
        Table(var_table_name),
        Join("__contains", "id", "=", "target_ref"),
        Join("observed-data", "source_ref", "=", "id"),
    ]

    if has_parent_ref:
        query_details.append(
            # put the LEFT JOIN at last, so no need to specify lhs for the first two JOINS
            Join(
                "process", "parent_ref", "=", "id", how="LEFT OUTER", lhs=var_table_name
            )
        )

    projection_details = [
        Column("id", var_table_name, "id"),
        Column("pid", var_table_name, "pid"),
        Column("name", var_table_name, "name"),
        "first_observed",
        "last_observed",
    ]

    if has_parent_ref:
        projection_details.append(Column("pid", "process", "ppid"))

    query_details.append(Projection(projection_details))

    query = Query(query_details)

    rows = store.run_query(query).fetchall()

    for row in rows:
        if row["pid"]:
            rid = row["id"]
            pname = row["name"]
            ppid = row["ppid"] if has_parent_ref else None
            try:
                st = to_datetime(row["first_observed"])
                ed = to_datetime(row["last_observed"])
            except Exception as e:
                _logger.error(f"query failed: {e!s}")
                st = datetime.datetime.now()
                ed = datetime.datetime.now()
            pid2procs[row["pid"]].append((rid, (pname, ppid, st, ed)))

    return pid2procs


def _search_for_potential_identical_process(ref_pid2procs, fil_pid2procs, config):
    # ref_pid2procs: {"pid":(rid, (proc_details))} for reference
    # fil_pid2procs: {"pid":(rid, (proc_details))} to search

    res_pid2procs = defaultdict(list)

    for pid, fil_procs in fil_pid2procs.items():
        for rid, fil_row in fil_procs:
            for _, ref_row in ref_pid2procs[pid]:
                if _identical_process_check(fil_row, ref_row, config):
                    res_pid2procs[pid].append((rid, fil_row))
                    break

    return res_pid2procs


def _identical_process_check(fil_row, ref_row, config):
    pbnc_bo = datetime.timedelta(
        seconds=config["pid_but_name_changed_time_begin_offset"]
    )
    pbnc_eo = datetime.timedelta(seconds=config["pid_but_name_changed_time_end_offset"])
    pan_bo = datetime.timedelta(seconds=config["pid_and_name_time_begin_offset"])
    pan_eo = datetime.timedelta(seconds=config["pid_and_name_time_end_offset"])
    pap_bo = datetime.timedelta(seconds=config["pid_and_ppid_time_begin_offset"])
    pap_eo = datetime.timedelta(seconds=config["pid_and_ppid_time_end_offset"])
    panap_bo = datetime.timedelta(
        seconds=config["pid_and_name_and_ppid_time_begin_offset"]
    )
    panap_eo = datetime.timedelta(
        seconds=config["pid_and_name_and_ppid_time_end_offset"]
    )

    fil_pname, fil_ppid, fil_start_time, fil_end_time = fil_row
    ref_pname, ref_ppid, ref_start_time, ref_end_time = ref_row
    if (
        (
            fil_pname
            and fil_ppid
            and fil_pname == ref_pname
            and fil_ppid == ref_ppid
            and fil_start_time > ref_start_time + panap_bo
            and fil_start_time < ref_end_time + panap_eo
        )
        or (
            fil_pname
            and fil_pname == ref_pname
            and fil_start_time > ref_start_time + pan_bo
            and fil_start_time < ref_end_time + pan_eo
        )
        or (
            fil_ppid
            and fil_ppid == ref_ppid
            and fil_start_time > ref_start_time + pap_bo
            and fil_start_time < ref_end_time + pap_eo
        )
        or (
            # name changed process, Linux fork+exec handled
            fil_start_time > ref_start_time + pbnc_bo
            and fil_start_time < ref_end_time + pbnc_eo
        )
        or (
            # name changed process, Linux fork+exec handled
            fil_end_time > ref_start_time + pbnc_bo
            and fil_end_time < ref_end_time + pbnc_eo
        )
    ):
        return True
    else:
        return False
