import resources
import os
import bq_utils
import re
import sql_wrangle
import logging

ACHILLES_HEEL_RESULTS = 'achilles_heel_results'
ACHILLES_RESULTS_DERIVED = 'achilles_results_derived'
ACHILLES_HEEL_TABLES = [ACHILLES_HEEL_RESULTS, ACHILLES_RESULTS_DERIVED]
PREFIX_PLACEHOLDER = 'synpuf_100.'
TEMP_PREFIX = 'temp.'
TEMP_TABLE_PATTERN = re.compile('\s*INTO\s+([^\s]+)')
SPLIT_PATTERN = ';zzzzzz'
TRUNCATE_TABLE_PATTERN = re.compile('\s*truncate\s+table\s+([^\s]+)')
DROP_TABLE_PATTERN = re.compile('\s*drop\s+table\s+([^\s]+)')

ACHILLES_HEEL_DML = os.path.join(resources.resource_path, 'achilles_heel_dml.sql')


def remove_sql_comment_from_string(string):
    """ takes a string of the form : part of query -- comment and returns only the former.

    :string: part of sql query -- comment type strings
    :returns: the part of the sql query

    """
    query_part = string.strip().split('--')[0].strip()
    return query_part


def _extract_sql_queries(heel_dml_path):
    all_query_parts_list = []  # pair (type, query/table_name)
    with open(heel_dml_path, 'r') as heel_script:
        for line in heel_script:
            part = remove_sql_comment_from_string(line)
            if part == '':
                continue
            all_query_parts_list.append(part)

    queries = []
    all_query_string = 'zzzzzz'.join(all_query_parts_list)
    for query in re.split(SPLIT_PATTERN, all_query_string):
        query = query.strip()
        query = query.replace('zzzzzz', ' ')
        if query != '':
            queries.append(query)

    return queries


def _get_heel_commands(hpo_id):
    raw_commands = _extract_sql_queries(ACHILLES_HEEL_DML)
    commands = map(lambda cmd: sql_wrangle.qualify_tables(cmd, hpo_id), raw_commands)
    for command in commands:
        yield command


def load_heel(hpo_id):
    commands = _get_heel_commands(hpo_id)
    for type, command in commands:
        bq_utils.query(command)


def run_heel(hpo_id):
    # very long test
    commands = _get_heel_commands(hpo_id)
    count = 0
    into_temp_count = 0
    current_temp_table = None
    running_query_ids = []
    for command in commands:
        count = count + 1
        logging.debug(' ---- submissing query # {}'.format(count))
        logging.debug(' ---- QUERY : `%s`...\n' % command)
        if sql_wrangle.is_to_temp_table(command):
            # wait for running query_ids to complete
            success_flag = bq_utils.wait_on_jobs(running_query_ids)
            if not success_flag:
                raise RuntimeError('Jobs take more than 30 seconds!')
            running_query_ids = []

            temp_table_name = sql_wrangle.get_temp_table_name(command)
            table_id = temp_table_name + str(into_temp_count)
            current_temp_table = temp_table_name + ""

            query = sql_wrangle.get_temp_table_query(command)
            insert_result = bq_utils.query(query, False, table_id)
            # wait for temp table insert job to complete
            bq_utils.wait_on_jobs([insert_result['jobReference']['jobId']], retry_count=10)

        elif sql_wrangle.is_truncate(command):
            table_id = sql_wrangle.get_truncate_table_name(command)
            if bq_utils.table_exists(table_id):
                bq_utils.delete_table(table_id)
        elif sql_wrangle.is_drop(command):
            table_id = sql_wrangle.get_drop_table_name(command)
            if bq_utils.table_exists(table_id):
                bq_utils.delete_table(table_id)
        else:
            if current_temp_table is not None:
                command = command.replace(current_temp_table, current_temp_table + str(into_temp_count))
            insert_result = bq_utils.query(command)
            running_query_ids.append(insert_result['jobReference']['jobId'])

    success_flag = bq_utils.wait_on_jobs(running_query_ids)
    if not success_flag:
        raise RuntimeError('Jobs take more than 30 seconds!')


def create_tables(hpo_id, drop_existing=False):
    """
    Create the achilles related tables
    :param hpo_id: associated hpo id
    :param drop_existing: if True, drop existing tables
    :return:
    """

    for table_name in ACHILLES_HEEL_TABLES:
        table_id = bq_utils.get_table_id(hpo_id, table_name)
        bq_utils.create_standard_table(table_name, table_id, drop_existing)
