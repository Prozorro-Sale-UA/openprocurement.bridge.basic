# -*- coding: utf-8 -*-
from gevent import monkey
monkey.patch_all()

import argparse
import logging
import logging.config
import math
import os
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from urlparse import urlparse

import gevent.pool
from gevent import sleep, spawn
from gevent.queue import PriorityQueue, Queue
from openprocurement_client.exceptions import RequestFailed
from openprocurement_client.resources.sync import ResourceFeeder
from openprocurement_client.resources.tenders import TendersClient as APIClient
from pkg_resources import iter_entry_points
from yaml import load

from openprocurement.bridge.basic.constants import DEFAULTS, PROCUREMENT_METHOD_TYPE_HANDLERS
from openprocurement.bridge.basic.utils import DataBridgeConfigError


try:
    import urllib3.contrib.pyopenssl
    urllib3.contrib.pyopenssl.inject_into_urllib3()
except ImportError:
    pass

logger = logging.getLogger(__name__)


class BasicDataBridge(object):

    """Basic Bridge"""

    def __init__(self, config):
        super(BasicDataBridge, self).__init__()
        defaults = deepcopy(DEFAULTS)
        defaults.update(config['main'])
        self.config = defaults
        # Init config
        for key, value in defaults.items():
            setattr(self, key, value)
        self.bridge_id = uuid.uuid4().hex
        self.api_host = self.config.get('resources_api_server')
        self.api_version = self.config.get('resources_api_version')
        self.retrievers_params = self.config.get('retrievers_params')
        self.storage_type = self.config['storage_config'].get('storage_type', 'couchdb')
        self.worker_type = self.config['worker_config'].get('worker_type', 'basic_couchdb')
        self.filter_type = self.config['filter_config'].get('filter_type', 'basic_couchdb')

        # Check up_wait_sleep
        up_wait_sleep = self.retrievers_params.get('up_wait_sleep')
        if up_wait_sleep is not None and up_wait_sleep < 30:
            raise DataBridgeConfigError(
                'Invalid \'up_wait_sleep\' in \'retrievers_params\'. Value must be grater than 30.')

        # Pools
        self.workers_pool = gevent.pool.Pool(self.workers_max)
        self.retry_workers_pool = gevent.pool.Pool(self.retry_workers_max)
        self.filter_workers_pool = gevent.pool.Pool(self.filter_workers_count)

        # Queues
        if self.input_queue_size == -1:
            self.input_queue = PriorityQueue()
        else:
            self.input_queue = PriorityQueue(self.input_queue_size)

        if self.resource_items_queue_size == -1:
            self.resource_items_queue = PriorityQueue()
        else:
            self.resource_items_queue = PriorityQueue(self.resource_items_queue_size)

        self.api_clients_queue = Queue()
        # self.retry_api_clients_queue = Queue()

        if self.retry_resource_items_queue_size == -1:
            self.retry_resource_items_queue = PriorityQueue()
        else:
            self.retry_resource_items_queue = PriorityQueue(self.retry_resource_items_queue_size)

        if self.api_host != '' and self.api_host is not None:
            api_host = urlparse(self.api_host)
            if api_host.scheme == '' and api_host.netloc == '':
                raise DataBridgeConfigError('Invalid \'resources_api_server\' url.')
        else:
            raise DataBridgeConfigError('In config dictionary empty or missing \'resources_api_server\'')

        # Connecting storage plugin
        self.db = None
        for entry_point in iter_entry_points('openprocurement.bridge.basic.storage_plugins', self.storage_type):
            plugin = entry_point.load()
            self.db = plugin(self.config)

        # Register handlers
        handlers = self.config.get('handlers', [])
        for entry_point in iter_entry_points('openprocurement.bridge.basic.handlers'):
            if not handlers or entry_point.name in handlers:
                plugin = entry_point.load()
                PROCUREMENT_METHOD_TYPE_HANDLERS[entry_point.name] = plugin(self.config, self.db)

        if hasattr(self, 'filter_type'):
            for entry_point in iter_entry_points('openprocurement.bridge.basic.filter_plugins', self.filter_type):
                self.filter_greenlet = entry_point.load()
        for entry_point in iter_entry_points('openprocurement.bridge.basic.worker_plugins', self.worker_type):
            self.worker_greenlet = entry_point.load()

        self.feeder = ResourceFeeder(host=self.api_host,
                                     version=self.api_version, key='',
                                     resource=self.config['resource'],
                                     extra_params=self.extra_params,
                                     retrievers_params=self.retrievers_params,
                                     adaptive=True, with_priority=True)
        self.api_clients_info = {}

    def create_api_client(self):
        client_user_agent = self.user_agent + '/' + self.bridge_id
        timeout = 0.1
        while 1:
            try:
                api_client = APIClient(
                    host_url=self.api_host, user_agent=client_user_agent, api_version=self.api_version, key='',
                    resource=self.resource
                )
                client_id = uuid.uuid4().hex
                logger.info(
                    'Started api_client {}'.format(api_client.session.headers['User-Agent']),
                    extra={'MESSAGE_ID': 'create_api_clients'})
                api_client_dict = {
                    'id': client_id,
                    'client': api_client,
                    'request_interval': 0,
                    'not_actual_count': 0
                }
                self.api_clients_info[api_client_dict['id']] = {
                    'drop_cookies': False,
                    'request_durations': {},
                    'request_interval': 0,
                    'avg_duration': 0
                }
                self.api_clients_queue.put(api_client_dict)
                break
            except RequestFailed as e:
                logger.error('Failed start api_client with status code {}'.format(e.status_code),
                             extra={'MESSAGE_ID': 'exceptions'})
                timeout = timeout * 2
                logger.info('create_api_client will be sleep {} sec.'.format(timeout))
                sleep(timeout)
            except Exception as e:
                logger.error('Failed start api client with error: {}'.format(e.message),
                             extra={'MESSAGE_ID': 'exceptions'})
                timeout = timeout * 2
                logger.info('create_api_client will be sleep {} sec.'.format(timeout))
                sleep(timeout)

    def fill_api_clients_queue(self):
        while self.api_clients_queue.qsize() < self.workers_min:
            self.create_api_client()

    def fill_input_queue(self):
        # if not hasattr(self.db, 'filter'):
        #     self.input_queue = self.resource_items_queue
        for resource_item in self.feeder.get_resource_items():
            self.input_queue.put(resource_item)
            logger.debug(
                'Add to temp queue from sync: {} {} {}'.format(self.resource[:-1],
                                                               resource_item[1]['id'],
                                                               resource_item[1]['dateModified']),
                extra={'MESSAGE_ID': 'received_from_sync', 'TEMP_QUEUE_SIZE': self.input_queue.qsize()}
            )

    def _get_average_requests_duration(self):
        req_durations = []
        delta = timedelta(seconds=self.perfomance_window)
        current_date = datetime.now() - delta
        for cid, info in self.api_clients_info.items():
            if len(info['request_durations']) > 0:
                if min(info['request_durations'].keys()) <= current_date:
                    info['grown'] = True
                avg = round(sum(info['request_durations'].values()) * 1.0 / len(info['request_durations']), 3)
                req_durations.append(avg)
                info['avg_duration'] = avg

        if len(req_durations) > 0:
            return round(sum(req_durations) / len(req_durations), 3), req_durations
        else:
            return 0, req_durations

    # TODO: Add logic for restart sync if last response grater than some values
    # and no active tasks specific for resource

    def queues_controller(self):
        while True:
            if (self.workers_pool.free_count() > 0 and
                (self.resource_items_queue.qsize() >
                 ((float(self.resource_items_queue_size) / 100) *
                  self.workers_inc_threshold))):
                self.create_api_client()
                w = self.worker_greenlet.spawn(self.api_clients_queue,
                                               self.resource_items_queue,
                                               self.db, self.config,
                                               self.retry_resource_items_queue,
                                               self.api_clients_info)
                self.workers_pool.add(w)
                logger.info('Queue controller: Create main queue worker.')
            elif (self.resource_items_queue.qsize() <
                  ((float(self.resource_items_queue_size) / 100) *
                   self.workers_dec_threshold)):
                if len(self.workers_pool) > self.workers_min:
                    wi = self.workers_pool.greenlets.pop()
                    wi.shutdown()
                    api_client_dict = self.api_clients_queue.get()
                    del self.api_clients_info[api_client_dict['id']]
                    logger.info('Queue controller: Kill main queue worker.')
            filled_resource_items_queue = round(self.resource_items_queue.qsize() /
                                                (float(self.resource_items_queue_size) / 100), 2)
            logger.info('Resource items queue filled on {} %'.format(filled_resource_items_queue))
            filled_retry_resource_items_queue \
                = round(self.retry_resource_items_queue.qsize() / float(self.retry_resource_items_queue_size) / 100, 2)
            logger.info('Retry resource items queue filled on {} %'.format(filled_retry_resource_items_queue))
            sleep(self.queues_controller_timeout)

    def gevent_watcher(self):
        self.perfomance_watcher()

        # Check fill threads
        input_threads = 1
        if self.input_queue_filler.ready():
            input_threads = 0
            logger.error('Temp queue filler error: {}'.format(self.input_queue_filler.exception.message),
                         extra={'MESSAGE_ID': 'exception'})
            self.input_queue_filler = spawn(self.fill_input_queue)
        logger.info('Input threads {}'.format(input_threads), extra={'INPUT_THREADS': input_threads})
        fill_threads = 1
        if hasattr(self, 'queue_filter') and self.queue_filter.ready():
            fill_threads = 0
            logger.error('Fill thread error: {}'.format(self.queue_filter.exception.message),
                         extra={'MESSAGE_ID': 'exception'})
            self.queue_filter = self.filter_greenlet.spawn(self.config, self.input_queue,
                                                           self.resource_items_queue, self.db)
        logger.info('Filter threads {}'.format(fill_threads), extra={'FILTER_THREADS': fill_threads})

        main_threads = self.workers_max - self.workers_pool.free_count()
        logger.info('Main threads {}'.format(main_threads), extra={'MAIN_THREADS': main_threads})

        if len(self.workers_pool) < self.workers_min:
            for i in xrange(0, (self.workers_min - len(self.workers_pool))):
                self.create_api_client()
                w = self.worker_greenlet.spawn(self.api_clients_queue,
                                               self.resource_items_queue,
                                               self.db, self.config,
                                               self.retry_resource_items_queue,
                                               self.api_clients_info)
                self.workers_pool.add(w)
                logger.info('Watcher: Create main queue worker.')
        retry_threads = self.retry_workers_max - self.retry_workers_pool.free_count()
        logger.info('Retry threads {}'.format(retry_threads), extra={'RETRY_THREADS': retry_threads})
        if len(self.retry_workers_pool) < self.retry_workers_min:
            for i in xrange(0, self.retry_workers_min - len(self.retry_workers_pool)):
                self.create_api_client()
                w = self.worker_greenlet.spawn(self.api_clients_queue,
                                               self.retry_resource_items_queue,
                                               self.db, self.config,
                                               self.retry_resource_items_queue,
                                               self.api_clients_info)
                self.retry_workers_pool.add(w)
                logger.info('Watcher: Create retry queue worker.')

        # Log queues size and API clients count
        main_queue_size = self.resource_items_queue.qsize()
        logger.info('Resource items queue size {}'.format(main_queue_size),
                    extra={'MAIN_QUEUE_SIZE': main_queue_size})
        retry_queue_size = self.retry_resource_items_queue.qsize()
        logger.info('Resource items retry queue size {}'.format(retry_queue_size),
                    extra={'RETRY_QUEUE_SIZE': retry_queue_size})
        api_clients_count = len(self.api_clients_info)
        logger.info('API Clients count: {}'.format(api_clients_count),
                    extra={'API_CLIENTS': api_clients_count})

    def _calculate_st_dev(self, values):
        if len(values) > 0:
            avg = sum(values) * 1.0 / len(values)
            variance = map(lambda x: (x - avg) ** 2, values)
            avg_variance = sum(variance) * 1.0 / len(variance)
            st_dev = math.sqrt(avg_variance)
            return round(st_dev, 3)
        else:
            return 0

    def _mark_bad_clients(self, dev):
        # Mark bad api clients
        for cid, info in self.api_clients_info.items():
            if info.get('grown', False) and info['avg_duration'] > dev:
                info['drop_cookies'] = True
                logger.debug(
                    'Perfomance watcher: Mark client {} as bad, avg. request_duration is {} sec.'.format(
                        cid, info['avg_duration']),
                    extra={'MESSAGE_ID': 'marked_as_bad'})
            elif info['avg_duration'] < dev and info['request_interval'] > 0:
                info['drop_cookies'] = True
                logger.debug(
                    'Perfomance watcher: Mark client {} as bad, request_interval is {} sec.'.format(
                        cid, info['request_interval']),
                    extra={'MESSAGE_ID': 'marked_as_bad'})

    def perfomance_watcher(self):
            avg_duration, values = self._get_average_requests_duration()
            for _, info in self.api_clients_info.items():
                delta = timedelta(
                    seconds=self.perfomance_window + self.watch_interval)
                current_date = datetime.now() - delta
                delete_list = []
                for key in info['request_durations']:
                    if key < current_date:
                        delete_list.append(key)
                for k in delete_list:
                    del info['request_durations'][k]
                delete_list = []

            st_dev = self._calculate_st_dev(values)
            if len(values) > 0:
                min_avg = min(values) * 1000
                max_avg = max(values) * 1000
            else:
                max_avg = 0
                min_avg = 0
            dev = round(st_dev + avg_duration, 3)

            logger.info(
                'Perfomance watcher:\nREQUESTS_STDEV - {} sec.\n'
                'REQUESTS_DEV - {} ms.\nREQUESTS_MIN_AVG - {} ms.\n'
                'REQUESTS_MAX_AVG - {} ms.\nREQUESTS_AVG - {} sec.'.format(
                    round(st_dev, 3), dev, min_avg, max_avg, avg_duration),
                extra={'REQUESTS_DEV': dev * 1000,
                       'REQUESTS_MIN_AVG': min_avg,
                       'REQUESTS_MAX_AVG': max_avg,
                       'REQUESTS_AVG': avg_duration * 1000})
            self._mark_bad_clients(dev)

    def run(self):
        logger.info('Start Basic Bridge', extra={'MESSAGE_ID': 'start_basic_bridge'})
        logger.info('Start data sync...', extra={'MESSAGE_ID': 'basic_bridge__data_sync'})
        self.input_queue_filler = spawn(self.fill_input_queue)
        if hasattr(self, 'filter_greenlet'):
            self.queue_filter = self.filter_greenlet.spawn(self.config, self.input_queue,
                                                           self.resource_items_queue, self.db)
        else:
            self.resource_items_queue = self.input_queue
        spawn(self.queues_controller)
        while True:
            self.gevent_watcher()
            sleep(self.watch_interval)


def main():
    parser = argparse.ArgumentParser(description='---- Basic Data Bridge ----')
    parser.add_argument('config', type=str, help='Path to configuration file')
    params = parser.parse_args()
    if os.path.isfile(params.config):
        with open(params.config) as config_file_obj:
            config = load(config_file_obj.read())
        logging.config.dictConfig(config)
        BasicDataBridge(config).run()


##############################################################

if __name__ == "__main__":
    main()
