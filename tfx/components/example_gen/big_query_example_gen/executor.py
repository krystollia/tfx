# Lint as: python2, python3
# Copyright 2019 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Generic TFX BigQueryExampleGen executor."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from typing import Any, Dict, List, Optional, Text

import apache_beam as beam
import tensorflow as tf

from google.cloud import bigquery
from tfx import types
from tfx.components.example_gen import base_example_gen_executor

# TODO(b/155637606): Move Big query example gen to GCP extensions.


class _BigQueryConverter(object):
  """Help class for bigquery result row to tf example conversion."""

  def __init__(self, query: Text):
    client = bigquery.Client()
    # Dummy query to get the type information for each field.
    query_job = client.query('SELECT * FROM ({}) LIMIT 0'.format(query))
    results = query_job.result()
    self._type_map = {}
    for field in results.schema:
      self._type_map[field.name] = field.field_type

  def RowToExample(self, instance: Dict[Text, Any]) -> tf.train.Example:
    """Convert bigquery result row to tf example."""
    feature = {}
    for key, value in instance.items():
      data_type = self._type_map[key]
      if value is None:
        feature[key] = tf.train.Feature()
      elif data_type in ('INTEGER', 'BOOLEAN'):
        feature[key] = tf.train.Feature(
            int64_list=tf.train.Int64List(value=[value]))
      elif data_type == 'FLOAT':
        feature[key] = tf.train.Feature(
            float_list=tf.train.FloatList(value=[value]))
      elif data_type == 'STRING':
        feature[key] = tf.train.Feature(
            bytes_list=tf.train.BytesList(value=[tf.compat.as_bytes(value)]))
      else:
        # TODO(jyzhao): support more types.
        raise RuntimeError(
            'BigQuery column type {} is not supported.'.format(data_type))
    return tf.train.Example(features=tf.train.Features(feature=feature))


# Create this instead of inline in _BigQueryToExample for test mocking purpose.
@beam.ptransform_fn
@beam.typehints.with_input_types(beam.Pipeline)
@beam.typehints.with_output_types(beam.typehints.Dict[Text, Any])
def _ReadFromBigQueryImpl(  # pylint: disable=invalid-name
    pipeline: beam.Pipeline,
    query: Text,
    project: Optional[Text],
    use_bigquery_source: bool = False) -> beam.pvalue.PCollection:
  """Read from BigQuery.

  Args:
    pipeline: beam pipeline.
    query: a BigQuery sql string.
    project: The ID of the project running this job.
    use_bigquery_source: Whether to use BigQuerySource instead of experimental
      `ReadFromBigQuery` PTransform.

  Returns:
    PCollection of dict.
  """
  # TODO(b/155441037): Consolidate to ReadFromBigQuery once its performance
  # on dataflow runner is on par with BigQuerySource.
  if use_bigquery_source:
    return (pipeline
            | 'ReadFromBigQuerySource' >> beam.io.Read(
                beam.io.BigQuerySource(query=query, use_standard_sql=True)))
  # TODO(b/155441037): Change this to top level import after Beam version is
  # upgraded to 2.21.
  try:
    from apache_beam.io.gcp.bigquery import ReadFromBigQuery  # pylint: disable=import-outside-toplevel,g-import-not-at-top
  except ImportError:
    from apache_beam.io.gcp.bigquery import _ReadFromBigQuery as ReadFromBigQuery  # pylint: disable=import-outside-toplevel,g-import-not-at-top
  return (pipeline
          | 'ReadFromBigQuery' >> ReadFromBigQuery(
              query=query, use_standard_sql=True, project=project))


@beam.ptransform_fn
@beam.typehints.with_input_types(beam.Pipeline)
@beam.typehints.with_output_types(tf.train.Example)
def _BigQueryToExample(  # pylint: disable=invalid-name
    pipeline: beam.Pipeline,
    input_dict: Dict[Text, List[types.Artifact]],  # pylint: disable=unused-argument
    exec_properties: Dict[Text, Any],  # pylint: disable=unused-argument
    split_pattern: Text) -> beam.pvalue.PCollection:
  """Read from BigQuery and transform to TF examples.

  Args:
    pipeline: beam pipeline.
    input_dict: Input dict from input key to a list of Artifacts.
    exec_properties: A dict of execution properties.
    split_pattern: Split.pattern in Input config, a BigQuery sql string.

  Returns:
    PCollection of TF examples.
  """
  converter = _BigQueryConverter(split_pattern)

  # TODO(b/155441037): Clean up the usage of `runner` flag
  # once ReadFromBigQuery performance on dataflow runner is on par
  # with BigQuerySource, and clean up `project` once beam is upgraded to 2.22.
  beam_pipeline_args = exec_properties['_beam_pipeline_args']
  pipeline_options = beam.options.pipeline_options.PipelineOptions(
      beam_pipeline_args)
  project = pipeline_options.get_all_options().get('project')
  use_dataflow_runner = pipeline_options.get_all_options().get('runner') in [
      'dataflow', 'DataflowRunner'
  ]

  return (pipeline
          | 'QueryTable' >> _ReadFromBigQueryImpl(  # pylint: disable=no-value-for-parameter
              query=split_pattern,
              project=project,
              use_bigquery_source=use_dataflow_runner)
          | 'ToTFExample' >> beam.Map(converter.RowToExample))


class Executor(base_example_gen_executor.BaseExampleGenExecutor):
  """Generic TFX BigQueryExampleGen executor."""

  def GetInputSourceToExamplePTransform(self) -> beam.PTransform:
    """Returns PTransform for BigQuery to TF examples."""
    return _BigQueryToExample
