#!/usr/bin/env python
# Author: Scott Haskell
# Company: Splunk Inc.
# Date: 2016-10-21
#
# The MIT License
#
# Copyright (c) 2016 Scott Haskell, Splunk Inc.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
# 
from pykafka import KafkaClient
import logging
import multiprocessing
from splunkhec import hec
import argparse
import sys
import yaml
from redo import retry

jobs = []

def parseArgs():
    """ Parse command line arguments 
    Returns:
    flags -- parsed command line arguments
    """
    argparser = argparse.ArgumentParser() 
    argparser.add_argument('-c',
                           '--config',
                           default='kafka_consumer.yml',
                           help='Kafka consumer config file')
    flags = argparser.parse_args()
    return(flags)

class kafkaConsumer:
    """ Kafka consumer class """


    def __init__(self,
                 brokers=[],
                 zookeeper_server="",
                 topic="",
                 consumer_group="",
                 use_rdkafka=False,
                 splunk_server="",
                 splunk_hec_port="8088",
                 splunk_hec_channel="",
                 splunk_hec_token="",
                 splunk_sourcetype="",
                 splunk_source="",
                 batch_size=1024,
                 retry_attempts=5,
                 sleeptime=60,
                 max_sleeptime=300,
                 sleepscale=1.5,
                 jitter=1,
                 loglevel="logging.WARNING"):
        """
        Keyword Arguments:
        brokers -- (list) of Kafka brokers <host>:<port>
        zookeeper_sever -- (string) zookeeper server of Kafka cluster
        topic -- (string) Kafka topic to be consumed
        consumer_group -- (string) arbitrary group name to manage balanced consumption of topic
        use_rdkafka -- (boolean) use librdkafka for speed increase
        splunk_server -- (string) hostname or IP address of HEC server or load balancer
        splunk_hec_port -- (string) port of Splunk HEC server (default 8088)
        splunk_hec_channel -- (string) UUID used by Splunk HEC on per application basis
        splunk_hec_token -- (string) UUID of token generated by Splunk for HEC input
        splunk_sourcetype -- (string) Splunk sourcetype for data source
        splunk_source -- (string) Splunk source
        batch_size -- (int) Number of messages to consume before attempting to send to Splunk HEC
        retry_attempts -- (int) Number of retry attempts before quitting (default 1024)
        sleeptime -- (int) Sleeptime between retries (default 60)
        max_sleeptime -- (int) Maximum sleeptime for any retry (default 300)
        sleepscale -- (float) Sleep time multiplier applied to each iteration
        jitter -- (int) random jitter introduced to each iteration, random between [-jitter, +jitter]
        loglevel -- (string) (logging.DEBUG|logging.INFO|logging.WARNING|logging.ERROR|logging.CRITICAL)

        Returns:
        kafkaConsumer class instance
        """
        self.messages = []
        self.brokers = brokers
        self.client = KafkaClient(hosts=','.join(self.brokers))
        self.zookeeper_server = zookeeper_server
        self.topic = topic
        self.consumer_group = consumer_group
        self.use_rdkafka = use_rdkafka
        self.splunk_server = splunk_server
        self.splunk_hec_port = splunk_hec_port
        self.splunk_hec_channel = splunk_hec_channel
        self.splunk_hec_token = splunk_hec_token
        self.splunk_sourcetype = splunk_sourcetype
        self.splunk_source = splunk_source
        self.batch_size = batch_size
        self.retry_attempts=retry_attempts
        self.sleeptime=sleeptime
        self.max_sleeptime=max_sleeptime
        self.sleepscale=sleepscale
        self.jitter=jitter
        self.loglevel=loglevel
        self.consumer_started=False
        self.initLogger()

    def initLogger(self):
        """ Initialize logger and set to loglevel"""
        log_format = '%(asctime)s name=%(name)s loglevel=%(levelname)s message=%(message)s'
        logging.basicConfig(format=log_format,
                            level=self.loglevel)

    def getConsumer(self, topic):
        """ Get a pykafka balanced consumer instance

        Arguments:
        topic -- (string) topic to be consumed

        Returns:
        pykafka balancedconsumer class instnace
        """

        consumer = topic.get_balanced_consumer(zookeeper_connect=self.zookeeper_server, 
                                               consumer_group=self.consumer_group,
                                               use_rdkafka=self.use_rdkafka)
        self.consumer_started = True

        return consumer

    def sendToSplunk(self,
                     splunk_hec):
        """ Send self.batch_size messages to Splunk and commit Kafka offsets if
        successful.

        Arguments:
        splunk_hec -- splunkhec class instance

        Returns:
        True if successful
        Raises Exception for non 200 status code
        """

        # Initialize and start consumer if down
        if(not self.consumer_started):
            self.consumer = self.getConsumer(self.client.topics[self.topic])

        # Attempt to send messages to Splunk
        status_code = splunk_hec.writeToHec(self.messages)

        # clear messages
        self.messages = []

        # Check for successful delivery
        if(status_code == 200):
            # commit offsets in Kafka
            self.consumer.commit_offsets()
            return
        else:
            # Stop consumer and mark it down
            self.consumer.stop()
            self.consumer_started = False

            # Raise exception for retry
            logging.error("Failed to send data to Splunk HTTP Event Collector - check host, port, token & channel")
            raise Exception('Failed to send data to Splunk HTTP Event Collector - Retrying')


    def consume(self):
        """ Start the consumer and read messages from Kafka sending self.batch_size messages
        to splunk in a perpetual loop. Attempt incremental backoff self.retry_attempts 
        times if any batch of messages gets a non 200 status code otherwise give up and exit.
        """

        self.consumer = self.getConsumer(self.client.topics[self.topic])

        # create splunk hec instance
        splunk_hec = hec(self.splunk_server,
                         self.splunk_hec_port,
                         self.splunk_hec_channel,
                         self.splunk_hec_token,
                         self.splunk_sourcetype,
                         self.splunk_source)
        while(True):
            m = self.consumer.consume()
            
            # Append messages to list until we've hit self.batch_size
            if(len(self.messages) <= self.batch_size):
                self.messages.append(m.value)
            # Send messages to Splunk HEC
            else:
                retry(self.sendToSplunk,
                      attempts=self.retry_attempts,
                      sleeptime=self.sleeptime,
                      max_sleeptime=self.max_sleeptime,
                      sleepscale=self.sleepscale,
                      jitter=self.jitter,
                      retry_exceptions=(Exception,),
                      args=(splunk_hec,))

def worker(num, config):
    """ Create kafkaConsumer instance and consume topic
    Arguments:
    config -- parsed YAML config
    """
    worker = "Worker-%s started" % (num)
    logging.info(worker)
    consumer = kafkaConsumer(config['kafka']['brokers'],
                             config['kafka']['zookeeper_server'],
                             config['kafka']['topic'],
                             config['kafka']['consumer_group'],
                             config['kafka']['use_rdkafka'],
                             config['hec']['host'],
                             config['hec']['port'],
                             config['hec']['channel'],
                             config['hec']['token'],
                             config['hec']['sourcetype'],
                             config['hec']['source'],
                             config['general']['batch_size'],
                             config['network']['retry_attempts'],
                             config['network']['sleeptime'],
                             config['network']['max_sleeptime'],
                             config['network']['sleepscale'],
                             config['network']['jitter'],
                             config['logging']['loglevel'])

    consumer.consume()

def parseConfig(config):
    """ Parse YAML config 
    Arguments:
    config -- (string) path to YAML config

    Returns:
    parsed YAML config file
    """
    with open(config, 'r') as stream:
        try:
            return(yaml.load(stream))
        except yaml.YAMLError as exc:
            print(exc)

def main():
    """ Parse args, parse YAML config, initialize workers and go. """
    flags = parseArgs()
    config = parseConfig(flags.config)
    multiprocessing.log_to_stderr(logging.INFO)
    num_workers = config['general']['workers']
    
    for i in range(num_workers):
        worker_name = "worker-%s" % i
        p = multiprocessing.Process(name=worker_name, target=worker, args=(i,config))
        jobs.append(p)
        p.start()

    for j in jobs:
        j.join()

if __name__ == '__main__':
    main()
