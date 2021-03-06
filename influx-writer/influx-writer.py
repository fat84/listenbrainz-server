#!/usr/bin/env python
from __future__ import print_function, division

import sys
import os
import pika
from datetime import datetime
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
from influxdb import InfluxDBClient
from influxdb.exceptions import InfluxDBClientError, InfluxDBServerError
import ujson
import logging
from listen import Listen
from time import time, sleep
import config
from listenstore import InfluxListenStore
from listenstore.utils import escape, get_measurement_name
from requests.exceptions import ConnectionError
from redis import Redis
from redis_keys import INCOMING_QUEUE_SIZE_KEY, UNIQUE_QUEUE_SIZE_KEY

REPORT_FREQUENCY = 5000
DUMP_JSON_WITH_ERRORS = False
ERROR_RETRY_DELAY = 3 # number of seconds to wait until retrying an operation


class InfluxWriterSubscriber(object):
    def __init__(self):
        self.log = logging.getLogger(__name__)
        logging.basicConfig()
        self.log.setLevel(logging.INFO)

        self.ls = None
        self.influx = None
        self.redis = None

        self.incoming_ch = None
        self.unique_ch = None
        self.connection = None
        self.total_inserts = 0
        self.inserts = 0
        self.time = 0


    @staticmethod
    def static_callback(ch, method, properties, body, obj):
        return obj.callback(ch, method, properties, body)


    def connect_to_rabbitmq(self):
        while True:
            try:
                self.connection = pika.BlockingConnection(pika.ConnectionParameters(host=config.RABBITMQ_HOST, port=config.RABBITMQ_PORT))
                break
            except Exception as e:
                self.log.error("Cannot connect to rabbitmq: %s, retrying in 2 seconds")
                sleep(ERROR_RETRY_DELAY)


    def callback(self, ch, method, properties, body):
        listens = ujson.loads(body)
        ret = self.write(listens)
        if not ret:
            return ret

        while True:
            try:
                self.incoming_ch.basic_ack(delivery_tag = method.delivery_tag)
                break
            except pika.exceptions.ConnectionClosed:
                self.connect_to_rabbitmq()

        count = len(listens)
        self.redis.decr(INCOMING_QUEUE_SIZE_KEY, count)

        # collect and occasionally print some stats
        self.inserts += count
        if self.inserts >= REPORT_FREQUENCY:
            self.total_inserts += self.inserts
            if self.time > 0:
                self.log.info("Inserted %d rows in %.1fs (%.2f listens/sec). Total %d rows." % \
                    (self.inserts, self.time, self.inserts / self.time, self.total_inserts))
            self.inserts = 0
            self.time = 0

        return ret


    def insert_to_listenstore(self, data, retries=5):
        """
        Inserts a batch of listens to the ListenStore. If this fails, then breaks the data into
        two parts and recursively tries to insert them, until we find the culprit listen

        Args:
            data: the data to be inserted into the ListenStore
            retries: the number of retries to make before deciding that we've failed

        Returns: number of listens successfully sent
        """

        if not data:
            return 0

        failure_count = 0
        while True:
            try:
                self.ls.insert(data)
                return len(data)
            except (InfluxDBServerError, InfluxDBClientError, ValueError) as e:
                failure_count += 1
                if failure_count >= retries:
                    break
                sleep(ERROR_RETRY_DELAY)
            except ConnectionError as e:
                self.log.error("Cannot write data to listenstore: %s. Sleep." % str(e))
                sleep(ERROR_RETRY_DELAY)

        # if we get here, we failed on trying to write the data
        if len(data) == 1:
            # try to send the bad listen one more time and if it doesn't work
            # log the error
            try:
                self.ls.insert(data)
                return 1
            except (InfluxDBServerError, InfluxDBClientError, ValueError, ConnectionError) as e:
                self.log.error("Unable to insert bad listen to listenstore: %s" % str(e))
                if DUMP_JSON_WITH_ERRORS:
                    self.log.error("Was writing the following data: ")
                    self.log.error(json.dumps(data, indent=4))
                return 0
        else:
            slice_index = len(data) // 2
            # send first half
            sent = self.insert_to_listenstore(data[:slice_index], retries)
            # send second half
            sent += self.insert_to_listenstore(data[slice_index:], retries)
            return sent


    def write(self, listen_dicts):
        submit = []
        unique = []
        duplicate_count = 0
        unique_count = 0

        # Partition the listens on the basis of user names
        # and then store the time range for each user
        users = {}
        for listen in listen_dicts:

            t = int(listen['listened_at'])
            user_name = listen['user_name']

            if user_name not in users:
                users[user_name] = {
                    'min_time': t,
                    'max_time': t,
                    'listens': [listen],
                }
                continue

            if t > users[user_name]['max_time']:
                users[user_name]['max_time'] = t

            if t < users[user_name]['min_time']:
                users[user_name]['min_time'] = t

            users[user_name]['listens'].append(listen)

        # get listens in the time range for each user and
        # remove duplicates on the basis of timestamps
        for user_name in users:

            min_time = users[user_name]['min_time']
            max_time = users[user_name]['max_time']

            # quering for artist name here, since a field must be included in the query.
            query = """SELECT time, artist_name
                         FROM "\\"%s\\""
                        WHERE time >= %d000000000
                          AND time <= %d000000000
                    """ % (escape(user_name), min_time, max_time)

            while True:
                try:
                    results = self.influx.query(query)
                    break
                except Exception as e:
                    self.log.error("Cannot query influx: %s" % str(e))
                    sleep(3)

            # collect all the timestamps for this given time range.
            timestamps = {}
            for result in results.get_points(measurement=get_measurement_name(user_name)):
                dt = datetime.strptime(result['time'] , "%Y-%m-%dT%H:%M:%SZ")
                timestamps[int(dt.strftime('%s'))] = 1

            for listen in users[user_name]['listens']:
                # Check to see if the timestamp is already in the DB
                t = int(listen['listened_at'])
                if t in timestamps:
                    duplicate_count += 1
                    continue

                unique_count += 1
                submit.append(Listen().from_json(listen))
                unique.append(listen)

        t0 = time()
        submitted_count = self.insert_to_listenstore(submit)
        self.time += time() - t0

        self.log.error("dups: %d, unique: %d, submitted: %d" % (duplicate_count, unique_count, submitted_count))
        if not unique_count:
            return True

        while True:
            try:
                self.unique_ch.basic_publish(exchange='unique', routing_key='', body=ujson.dumps(unique),
                    properties=pika.BasicProperties(delivery_mode = 2,))
                break
            except pika.exceptions.ConnectionClosed:
                self.connect_to_rabbitmq()

        self.redis.incr(UNIQUE_QUEUE_SIZE_KEY, unique_count)

        return True

    def start(self):
        self.log.info("influx-writer init")

        if not hasattr(config, "REDIS_HOST"):
            self.log.error("Redis service not defined. Sleeping 2 seconds and exiting.")
            sleep(ERROR_RETRY_DELAY)
            sys.exit(-1)

        if not hasattr(config, "INFLUX_HOST"):
            self.log.error("Influx service not defined. Sleeping 2 seconds and exiting.")
            sleep(ERROR_RETRY_DELAY)
            sys.exit(-1)

        if not hasattr(config, "RABBITMQ_HOST"):
            self.log.error("RabbitMQ service not defined. Sleeping 2 seconds and exiting.")
            sleep(ERROR_RETRY_DELAY)
            sys.exit(-1)

        while True:
            try:
                self.ls = InfluxListenStore({ 'REDIS_HOST' : config.REDIS_HOST,
                                         'REDIS_PORT' : config.REDIS_PORT,
                                         'INFLUX_HOST': config.INFLUX_HOST,
                                         'INFLUX_PORT': config.INFLUX_PORT,
                                         'INFLUX_DB_NAME': config.INFLUX_DB_NAME})
                self.influx = InfluxDBClient(host=config.INFLUX_HOST, port=config.INFLUX_PORT, database=config.INFLUX_DB_NAME)
                break
            except Exception as err:
                self.log.error("Cannot connect to influx: %s. Retrying in 2 seconds and trying again." % str(err))
                sleep(ERROR_RETRY_DELAY)

        while True:
            try:
                self.redis = Redis(host=config.REDIS_HOST, port=config.REDIS_PORT)
                self.redis.ping()
                break
            except Exception as err:
                self.log.error("Cannot connect to redis: %s. Retrying in 2 seconds and trying again." % str(err))
                sleep(ERROR_RETRY_DELAY)

        while True:
            self.connect_to_rabbitmq()
            self.incoming_ch = self.connection.channel()
            self.incoming_ch.exchange_declare(exchange='incoming', type='fanout')
            self.incoming_ch.queue_declare('incoming', durable=True)
            self.incoming_ch.queue_bind(exchange='incoming', queue='incoming')
            self.incoming_ch.basic_consume(lambda ch, method, properties, body: self.static_callback(ch, method, properties, body, obj=self), queue='incoming')

            self.unique_ch = self.connection.channel()
            self.unique_ch.exchange_declare(exchange='unique', type='fanout')
            self.unique_ch.queue_declare('unique', durable=True)

            self.log.info("influx-writer started")
            try:
                self.incoming_ch.start_consuming()
            except pika.exceptions.ConnectionClosed:
                self.log.info("Connection to rabbitmq closed. Re-opening.")
                self.connection = None
                self.channel = None
                continue

            self.connection.close()

    def print_and_log_error(self, msg):
        self.log.error(msg)
        print(msg, file = sys.stderr)

if __name__ == "__main__":
    rc = InfluxWriterSubscriber()
    rc.start()
