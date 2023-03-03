# (C) Datadog, Inc. 2023-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
from confluent_kafka import Consumer, ConsumerGroupTopicPartitions, KafkaException, TopicPartition
from confluent_kafka.admin import AdminClient
from six import string_types

from datadog_checks.base import ConfigurationError
from datadog_checks.kafka_consumer.client.kafka_client import KafkaClient
from datadog_checks.kafka_consumer.constants import KAFKA_INTERNAL_TOPICS


class ConfluentKafkaClient(KafkaClient):
    @property
    def kafka_client(self):
        if self._kafka_client is None:
            config = {
                "bootstrap.servers": self.config._kafka_connect_str,
                "socket.timeout.ms": self.config._request_timeout_ms,
                "client.id": "dd-agent",
                "security.protocol": self.config._security_protocol,
            }

            if self.config._sasl_mechanism == "OAUTHBEARER":
                assert (
                    self.config._sasl_oauth_token_provider is not None
                ), "sasl_oauth_token_provider required for OAUTHBEARER sasl"

                if self.config._sasl_oauth_token_provider.get("url") is None:
                    raise ConfigurationError("The `url` setting of `auth_token` reader is required")

                elif self.config._sasl_oauth_token_provider.get("client_id") is None:
                    raise ConfigurationError("The `client_id` setting of `auth_token` reader is required")

                elif self.config._sasl_oauth_token_provider.get("client_secret") is None:
                    raise ConfigurationError("The `client_secret` setting of `auth_token` reader is required")

                oauth_config = {
                    "sasl.mechanism": self.config._sasl_mechanism,
                    "sasl.oauthbearer.method": "oidc",
                    "sasl.oauthbearer.client.id": self.config._sasl_oauth_token_provider.get("client_id"),
                    "sasl.oauthbearer.token.endpoint.url": self.config._sasl_oauth_token_provider.get("url"),
                    "sasl.oauthbearer.client.secret": self.config._sasl_oauth_token_provider.get("client_secret"),
                }

                config.update(oauth_config)

            if self.config._sasl_mechanism == "GSSAPI":
                kerb_config = {
                    "sasl.mechanism": self.config._sasl_mechanism,
                    "sasl.username": self.config._sasl_plain_username,
                    "sasl.password": self.config._sasl_plain_password,
                    "sasl.kerberos.service.name": self.config._sasl_kerberos_service_name,
                    # TODO: _sasl_kerberos_domain_name doesn't seem to be used?
                    # "TBD": self.config._sasl_kerberos_domain_name,
                    "sasl.kerberos.keytab": "test",
                }
                config.update(kerb_config)

            self._kafka_client = AdminClient(config)
        return self._kafka_client

    def create_kafka_admin_client(self):
        raise NotImplementedError

    def get_consumer_offsets_dict(self):
        return self._consumer_offsets

    def get_highwater_offsets(self, consumer_offsets):
        # TODO: Remove broker_requests_batch_size as config after
        # kafka-python is removed if we don't need to batch requests in Confluent
        topics_with_consumer_offset = {}
        if not self.config._monitor_all_broker_highwatermarks:
            topics_with_consumer_offset = {(topic, partition) for (_, topic, partition) in consumer_offsets}

        # TODO: This is only to keep the same functionality as the original implementation,
        # since the kafka-python version needed to get the list of brokers to get the highwater offsets.
        # However, we don't need to do this in the Confluent implementation anymore,
        # since there's a specific function get_watermark_offsets() that calculates the offsets.
        # We still need to raise exceptions if the brokers are not fetched, since the tests assert this.
        if not self.kafka_client.list_topics(timeout=1).brokers:
            raise Exception()

        # TODO: We are still failing test_oauth_config and test_gssapi tests
        # since we haven't implemented OAuth/Kerberos support yet,
        # so the AdminClient is not configured for those tests yet.
        for consumer_group in consumer_offsets.items():
            consumer_config = {
                "bootstrap.servers": self.config._kafka_connect_str,
                "group.id": consumer_group,
            }
            consumer = Consumer(consumer_config)
            topics = consumer.list_topics()

            for topic in topics.topics:
                topic_partitions = [
                    TopicPartition(topic, partition) for partition in list(topics.topics[topic].partitions.keys())
                ]

                for topic_partition in topic_partitions:
                    partition = topic_partition.partition
                    if topic not in KAFKA_INTERNAL_TOPICS and (
                        self.config._monitor_all_broker_highwatermarks
                        or (topic, partition) in topics_with_consumer_offset
                    ):
                        _, high_offset = consumer.get_watermark_offsets(topic_partition)

                        self._highwater_offsets[(topic, partition)] = high_offset

    def get_highwater_offsets_dict(self):
        return self._highwater_offsets

    def reset_offsets(self):
        self._consumer_offsets = {}
        self._highwater_offsets = {}

    def get_partitions_for_topic(self, topic):

        try:
            cluster_metadata = self.kafka_client.list_topics(topic)
            topic_metadata = cluster_metadata.topics[topic]
            partitions = list(topic_metadata.partitions.keys())
            return partitions
        except KafkaException as e:
            self.log.error("Received exception when getting partitions for topic %s: %s", topic, e)
            return None

    def request_metadata_update(self):
        raise NotImplementedError

    def get_consumer_offsets(self):
        # {(consumer_group, topic, partition): offset}
        offset_futures = []

        if self.config._monitor_unlisted_consumer_groups:
            # Get all consumer groups
            consumer_groups = []
            consumer_groups_future = self.kafka_client.list_consumer_groups()
            self.log.debug('MONITOR UNLISTED CG FUTURES: %s', consumer_groups_future)
            try:
                list_consumer_groups_result = consumer_groups_future.result()
                self.log.debug('MONITOR UNLISTED FUTURES RESULT: %s', list_consumer_groups_result)
                for valid_consumer_group in list_consumer_groups_result.valid:
                    consumer_group = valid_consumer_group.group_id
                    topics = self.kafka_client.list_topics()
                    consumer_groups.append(consumer_group)
            except Exception as e:
                self.log.error("Failed to collect consumer offsets %s", e)

        elif self.config._consumer_groups:
            self._validate_consumer_groups()
            consumer_groups = self.config._consumer_groups

        else:
            raise ConfigurationError(
                "Cannot fetch consumer offsets because no consumer_groups are specified and "
                "monitor_unlisted_consumer_groups is %s." % self.config._monitor_unlisted_consumer_groups
            )

        topics = self.kafka_client.list_topics()

        for consumer_group in consumer_groups:
            self.log.debug('CONSUMER GROUP: %s', consumer_group)
            topic_partitions = self._get_topic_partitions(topics, consumer_group)
            for topic_partition in topic_partitions:
                offset_futures.append(
                    self.kafka_client.list_consumer_group_offsets(
                        [ConsumerGroupTopicPartitions(consumer_group, [topic_partition])]
                    )[consumer_group]
                )

        for future in offset_futures:
            try:
                response_offset_info = future.result()
                self.log.debug('FUTURE RESULT: %s', response_offset_info)
                consumer_group = response_offset_info.group_id
                topic_partitions = response_offset_info.topic_partitions
                self.log.debug('RESULT CONSUMER GROUP: %s', consumer_group)
                self.log.debug('RESULT TOPIC PARTITIONS: %s', topic_partitions)
                for topic_partition in topic_partitions:
                    topic = topic_partition.topic
                    partition = topic_partition.partition
                    offset = topic_partition.offset
                    self.log.debug('RESULTS TOPIC: %s', topic)
                    self.log.debug('RESULTS PARTITION: %s', partition)
                    self.log.debug('RESULTS OFFSET: %s', offset)

                    if topic_partition.error:
                        self.log.debug(
                            "Encountered error: %s. Occurred with topic: %s; partition: [%s]",
                            topic_partition.error.str(),
                            topic_partition.topic,
                            str(topic_partition.partition),
                        )
                    self._consumer_offsets[(consumer_group, topic, partition)] = offset
            except KafkaException as e:
                self.log.debug("Failed to read consumer offsets for %s: %s", consumer_group, e)

    def _validate_consumer_groups(self):
        """Validate any explicitly specified consumer groups.
        consumer_groups = {'consumer_group': {'topic': [0, 1]}}
        """
        assert isinstance(self.config._consumer_groups, dict)
        for consumer_group, topics in self.config._consumer_groups.items():
            assert isinstance(consumer_group, string_types)
            assert isinstance(topics, dict) or topics is None  # topics are optional
            if topics is not None:
                for topic, partitions in topics.items():
                    assert isinstance(topic, string_types)
                    assert isinstance(partitions, (list, tuple)) or partitions is None  # partitions are optional
                    if partitions is not None:
                        for partition in partitions:
                            assert isinstance(partition, int)

    def _get_topic_partitions(self, topics, consumer_group):
        topic_partitions = []
        for topic in topics.topics:
            if topic in KAFKA_INTERNAL_TOPICS:
                continue
            self.log.debug('CONFIGURED TOPICS: %s', topic)

            partitions = list(topics.topics[topic].partitions.keys())

            for partition in partitions:
                # Get all topic-partition combinations allowed based on config
                # if topics is None => collect all topics and partitions for the consumer group
                # if partitions is None => collect all partitions from the consumer group's topic
                if not self.config._monitor_unlisted_consumer_groups and self.config._consumer_groups.get(
                    consumer_group
                ):
                    if (
                        self.config._consumer_groups[consumer_group]
                        and topic not in self.config._consumer_groups[consumer_group]
                    ):
                        continue
                    if (
                        self.config._consumer_groups[consumer_group].get(topic)
                        and partition not in self.config._consumer_groups[consumer_group][topic]
                    ):
                        continue
                self.log.debug("TOPIC PARTITION: %s", TopicPartition(topic, partition))
                topic_partitions.append(TopicPartition(topic, partition))

        return topic_partitions

    def get_broker_offset(self):
        raise NotImplementedError