import sys
import time
import json
import uuid
import logging
import requests
from queue import Queue, Empty
from threading import Thread
from collections import defaultdict

from funcx import FuncXClient
from funcx.serialize import FuncXSerializer
from utils import colored, endpoint_name
from transfer import TransferManager
from strategies import init_strategy
from predictors import init_runtime_predictor, TransferPredictor, \
    ImportPredictor


logger = logging.getLogger(__name__)
ch = logging.StreamHandler()
ch.setFormatter(logging.Formatter(
    colored("[SCHEDULER] %(message)s", 'yellow')))
logger.addHandler(ch)


FUNCX_API = 'https://funcx.org/api/v1'
HEARTBEAT_THRESHOLD = 75.0  # Endpoints send regular heartbeats
CLIENT_ID = 'f06739da-ad7d-40bd-887f-abb1d23bbd6f'
BLOCK_ERRORS = [ModuleNotFoundError, MemoryError]


class CentralScheduler(object):

    def __init__(self, endpoints, strategy='round-robin',
                 runtime_predictor='rolling-average', last_n=3, train_every=1,
                 log_level='INFO', import_model_file=None,
                 transfer_model_file=None, sync_level='exists',
                 max_backups=0, backup_delay_threshold=2.0,
                 *args, **kwargs):
        self._fxc = FuncXClient(*args, **kwargs)

        # Initialize a transfer client
        self._transfer_manger = TransferManager(endpoints=endpoints,
                                                sync_level=sync_level,
                                                log_level=log_level)

        # Info about FuncX endpoints we can execute on
        self._endpoints = endpoints
        self._dead_endpoints = set()
        self.last_result_time = defaultdict(float)
        self.temperature = defaultdict(lambda: 'WARM')
        self._imports = defaultdict(list)
        self._imports_required = defaultdict(list)

        # Track which endpoints a function can't run on
        self._blocked = defaultdict(set)

        # Track pending tasks
        # We will provide the client our own task ids, since we may submit the
        # same task multiple times to the FuncX service, and sometimes we may
        # wait to submit a task to FuncX (e.g., wait for a data transfer).
        self._task_id_translation = {}
        self._pending = {}
        self._pending_by_endpoint = defaultdict(set)
        self._task_info = {}
        # List of endpoints a (virtual) task was scheduled to
        self._endpoints_sent_to = defaultdict(list)
        self.max_backups = max_backups
        self.backup_delay_threshold = backup_delay_threshold
        self._latest_status = {}
        self._last_task_ETA = defaultdict(float)
        # Maximum ETA, if any, of a task which we allow to be scheduled on an
        # endpoint. This is to prevent backfill tasks to be longer than the
        # estimated time for when a pending data transfer will finish.
        self._transfer_ETAs = defaultdict(dict)
        # Estimated error in the pending-task time of an endpoint.
        # Updated every time a task result is received from an endpoint.
        self._queue_error = defaultdict(float)

        # Set logging levels
        logger.setLevel(log_level)
        self.execution_log = []

        # Intialize serializer
        self.fx_serializer = FuncXSerializer()
        self.fx_serializer.use_custom('03\n', 'code')

        # Initialize runtime predictor
        self.runtime = init_runtime_predictor(runtime_predictor,
                                              endpoints=endpoints,
                                              last_n=last_n,
                                              train_every=train_every)
        logger.info(f"Runtime predictor using strategy {self.runtime}")

        # Initialize transfer-time predictor
        self.transfer_time = TransferPredictor(endpoints=endpoints,
                                               train_every=train_every,
                                               state_file=transfer_model_file)

        # Initialize import-time predictor
        self.import_predictor = ImportPredictor(endpoints=endpoints,
                                                state_file=import_model_file)

        # Initialize scheduling strategy
        self.strategy = init_strategy(strategy, endpoints=endpoints,
                                      runtime_predictor=self.runtime,
                                      queue_predictor=self.queue_delay,
                                      cold_start_predictor=self.cold_start,
                                      transfer_predictor=self.transfer_time)
        logger.info(f"Scheduler using strategy {self.strategy}")

        # Start thread to check on endpoints regularly
        self._endpoint_watchdog = Thread(target=self._check_endpoints)
        self._endpoint_watchdog.start()

        # Start thread to monitor tasks and send tasks to FuncX service
        self._scheduled_tasks = Queue()
        self._task_watchdog_sleep = 0.15
        self._task_watchdog = Thread(target=self._monitor_tasks)
        self._task_watchdog.start()

    def block(self, func, endpoint):
        if endpoint not in self._endpoints:
            logger.error('Cannot block unknown endpoint {}'
                         .format(endpoint))
            return {
                'status': 'Failed',
                'reason': 'Unknown endpoint {}'.format(endpoint)
            }
        elif len(self._blocked[func]) == len(self._endpoints) - 1:
            logger.error('Cannot block last remaining endpoint {}'
                         .format(endpoint))
            return {
                'status': 'Failed',
                'reason': 'Cannot block all endpoints for {}'.format(func)
            }
        else:
            logger.info('Blocking endpoint {} for function {}'
                        .format(endpoint_name(endpoint), func))
            self._blocked[func].add(endpoint)
            return {'status': 'Success'}

    def register_imports(self, func, imports):
        logger.info('Registered function {} with imports {}'
                    .format(func, imports))
        self._imports_required[func] = imports

    def batch_submit(self, tasks, headers):
        # TODO: smarter scheduling for batch submissions

        task_ids = []
        endpoints = []

        for func, payload in tasks:
            _, ser_kwargs = self.fx_serializer.unpack_buffers(payload)
            kwargs = self.fx_serializer.deserialize(ser_kwargs)
            files = kwargs['_globus_files']

            task_id, endpoint = self._schedule_task(func=func,
                                                    payload=payload,
                                                    headers=headers,
                                                    files=files)
            task_ids.append(task_id)
            endpoints.append(endpoint)

        return task_ids, endpoints

    def _schedule_task(self, func, payload, headers, files,
                       task_id=None):

        # If this is the first time scheduling this task_id
        # (i.e., non-backup task), record the necessary metadata
        if task_id is None:
            # Create (fake) task id to return to client
            task_id = str(uuid.uuid4())

            # Store task information
            self._task_id_translation[task_id] = set()

            # Information required to schedule the task, now and in the future
            info = {
                'function_id': func,
                'payload': payload,
                'headers': headers,
                'files': files,
                'time_requested': time.time()
            }
            self._task_info[task_id] = info

        # TODO: do not choose a dead endpoint (reliably)
        # exclude = self._blocked[func] | self._dead_endpoints | set(self._endpoints_sent_to[task_id])  # noqa
        if len(self._dead_endpoints) > 0:
            logger.warn('{} endpoints seem dead. Hope they still work!'
                        .format(len(self._dead_endpoints)))
        exclude = self._blocked[func] | set(self._endpoints_sent_to[task_id])
        choice = self.strategy.choose_endpoint(func, payload=payload,
                                               files=files,
                                               exclude=exclude,
                                               transfer_ETAs=self._transfer_ETAs)  # noqa
        endpoint = choice['endpoint']
        logger.info('Choosing endpoint {} for func {}, task id {}'
                    .format(endpoint_name(endpoint), func, task_id))
        choice['ETA'] = self.strategy.predict_ETA(func, endpoint, payload,
                                                  files=files)

        # Start Globus transfer of required files, if any
        if len(files) > 0:
            transfer_num = self._transfer_manger.transfer(files, endpoint,
                                                          task_id)
            if transfer_num is not None:
                transfer_ETA = time.time() + self.transfer_time(files, endpoint)
                self._transfer_ETAs[endpoint][transfer_num] = transfer_ETA
        else:
            transfer_num = None
            # Record endpoint ETA for queue-delay prediction here,
            # since task will be immediately scheduled
            self._last_task_ETA[endpoint] = choice['ETA']

        # If a cold endpoint is being started, mark it as no longer cold,
        # so that subsequent launch-time predictions are correct (i.e., 0)
        if self.temperature[endpoint] == 'COLD':
            self.temperature[endpoint] = 'WARMING'
            logger.info('A cold endpoint {} was chosen; marked as warming.'
                        .format(endpoint_name(endpoint)))

        # Schedule task for sending to FuncX
        self._endpoints_sent_to[task_id].append(endpoint)
        self._scheduled_tasks.put((task_id, endpoint, transfer_num))

        return task_id, endpoint

    def translate_task_id(self, task_id):
        return self._task_id_translation[task_id]

    def log_status(self, real_task_id, data):
        if real_task_id not in self._pending:
            logger.warn('Ignoring unknown task id {}'.format(real_task_id))
            return

        task_id = self._pending[real_task_id]['task_id']
        func = self._pending[real_task_id]['function_id']
        endpoint = self._pending[real_task_id]['endpoint_id']
        # Don't overwrite latest status if it is a result/exception
        if task_id not in self._latest_status or \
                self._latest_status[task_id].get('status') == 'PENDING':
            self._latest_status[task_id] = data

        if 'result' in data:
            result = self.fx_serializer.deserialize(data['result'])
            runtime = result['runtime']
            name = endpoint_name(endpoint)
            logger.info('Got result from {} for task {} with time {}'
                        .format(name, real_task_id, runtime))

            self.runtime.update(self._pending[real_task_id], runtime)
            self._pending[real_task_id]['runtime'] = runtime
            self._record_completed(real_task_id)
            self.last_result_time[endpoint] = time.time()
            self._imports[endpoint] = result['imports']

        elif 'exception' in data:
            exception = self.fx_serializer.deserialize(data['exception'])
            try:
                exception.reraise()
            except Exception as e:
                logger.error('Got exception on task {}: {}'
                             .format(real_task_id, e))
                exc_type, _, _ = sys.exc_info()
                if exc_type in BLOCK_ERRORS:
                    self.block(func, endpoint)

            self._record_completed(real_task_id)
            self.last_result_time[endpoint] = time.time()

        elif 'status' in data and data['status'] == 'PENDING':
            pass

        else:
            logger.error('Unexpected status message: {}'.format(data))

    def get_status(self, task_id):
        if task_id not in self._task_id_translation:
            logger.warn('Unknown client task id {}'.format(task_id))

        elif len(self._task_id_translation[task_id]) == 0:
            return {'status': 'PENDING'}  # Task has not been scheduled yet

        elif task_id not in self._latest_status:
            return {'status': 'PENDING'}  # Status has not been queried yet

        else:
            return self._latest_status[task_id]

    def queue_delay(self, endpoint):
        # Otherwise, queue delay is the ETA of most recent task,
        # plus the estimated error in the ETA prediction.
        # Note that if there are no pending tasks on endpoint, no queue delay.
        # This is implicit since, in this case, both summands will be 0.
        delay = self._last_task_ETA[endpoint] + self._queue_error[endpoint]
        return max(delay, time.time())

    def _record_completed(self, real_task_id):
        info = self._pending[real_task_id]
        endpoint = info['endpoint_id']

        # If this is the last pending task on this endpoint, reset ETA offset
        if len(self._pending_by_endpoint[endpoint]) == 1:
            self._last_task_ETA[endpoint] = 0.0
            self._queue_error[endpoint] = 0.0
        else:
            prediction_error = time.time() - self._pending[real_task_id]['ETA']
            self._queue_error[endpoint] = prediction_error
            # print(colored(f'Prediction error {prediction_error}', 'red'))

        info['ATA'] = time.time()
        del info['headers']
        self.execution_log.append(info)

        logger.info('Task exec time: expected = {:.3f}, actual = {:.3f}'
                    .format(info['ETA'] - info['time_sent'],
                            time.time() - info['time_sent']))
        # logger.info(f'ETA_offset = {self._queue_error[endpoint]:.3f}')

        # Stop tracking this task
        del self._pending[real_task_id]
        self._pending_by_endpoint[endpoint].remove(real_task_id)
        if info['task_id'] in self._task_info:
            del self._task_info[info['task_id']]

    def cold_start(self, endpoint, func):
        # If endpoint is warm, there is no launch time
        if self.temperature[endpoint] != 'COLD':
            launch_time = 0.0
        # Otherwise, return the launch time in the endpoint config
        elif 'launch_time' in self._endpoints[endpoint]:
            launch_time = self._endpoints[endpoint]['launch_time']
        else:
            logger.warn('Endpoint {} should always be warm, but is cold'
                        .format(endpoint_name(endpoint)))
            launch_time = 0.0

        # Time to import dependencies
        import_time = 0.0
        for pkg in self._imports_required[func]:
            if pkg not in self._imports[endpoint]:
                logger.debug('Cold-start has import time for pkg {} on {}'
                             .format(pkg, endpoint_name(endpoint)))
                import_time += self.import_predictor(pkg, endpoint)

        return launch_time + import_time

    def _monitor_tasks(self):
        logger.info('Starting task-watchdog thread')

        scheduled = {}

        while True:

            time.sleep(self._task_watchdog_sleep)

            # Get newly scheduled tasks
            while True:
                try:
                    task_id, end, num = self._scheduled_tasks.get_nowait()
                    if task_id not in self._task_info:
                        logger.warn('Task id {} scheduled but no info found'
                                    .format(task_id))
                        continue
                    info = self._task_info[task_id]
                    scheduled[task_id] = dict(info)  # Create new copy of info
                    scheduled[task_id]['task_id'] = task_id
                    scheduled[task_id]['endpoint_id'] = end
                    scheduled[task_id]['transfer_num'] = num
                except Empty:
                    break

            # Filter out all tasks whose data transfer has not been completed
            ready_to_send = set()
            for task_id, info in scheduled.items():
                transfer_num = info['transfer_num']
                if transfer_num is None:
                    ready_to_send.add(task_id)
                    info['transfer_time'] = 0.0
                elif self._transfer_manger.is_complete(transfer_num):
                    ready_to_send.add(task_id)
                    del self._transfer_ETAs[info['endpoint_id']][transfer_num]
                    info['transfer_time'] = self._transfer_manger.get_transfer_time(transfer_num)  # noqa
                else:  # This task cannot be scheduled yet
                    continue

            if len(ready_to_send) == 0:
                logger.debug('No new tasks to send. Task watchdog sleeping...')
                continue

            # TODO: different clients send different headers. change eventually
            headers = list(scheduled.values())[0]['headers']

            logger.info('Scheduling a batch of {} tasks'
                        .format(len(ready_to_send)))

            # Submit all ready tasks to FuncX
            data = {'tasks': []}
            for task_id in ready_to_send:
                info = scheduled[task_id]
                submit_info = (info['function_id'], info['endpoint_id'],
                               info['payload'])
                data['tasks'].append(submit_info)

            res_str = requests.post(f'{FUNCX_API}/submit', headers=headers,
                                    data=json.dumps(data))
            try:
                res = res_str.json()
            except ValueError:
                logger.error(f'Could not parse JSON from {res_str.text}')
                continue
            if res['status'] != 'Success':
                logger.error('Could not send tasks to FuncX. Got response: {}'
                             .format(res))
                continue

            # Update task info with submission info
            for task_id, real_task_id in zip(ready_to_send, res['task_uuids']):
                info = scheduled[task_id]
                # This ETA calculation does not take into account transfer time
                # since, at this point, the transfer has already completed.
                info['ETA'] = self.strategy.predict_ETA(info['function_id'],
                                                        info['endpoint_id'],
                                                        info['payload'])
                # Record if this ETA prediction is "reliable". If it is not
                # (e.g., when we have not learned about this (func, ep) pair),
                # backup tasks will not be sent for this task if it is delayed.
                info['is_ETA_reliable'] = self.runtime.has_learned(
                    info['function_id'], info['endpoint_id'])

                info['time_sent'] = time.time()

                endpoint = info['endpoint_id']
                self._task_id_translation[task_id].add(real_task_id)

                self._pending[real_task_id] = info
                self._pending_by_endpoint[endpoint].add(real_task_id)

                # Record endpoint ETA for queue-delay prediction
                self._last_task_ETA[endpoint] = info['ETA']

                logger.info('Sent task id {} to {} with real task id {}'
                            .format(task_id, endpoint_name(endpoint),
                                    real_task_id))

            # Stop tracking all newly sent tasks
            for task_id in ready_to_send:
                del scheduled[task_id]

    def _check_endpoints(self):
        logger.info('Starting endpoint-watchdog thread')

        while True:
            for end in self._endpoints.keys():
                statuses = self._fxc.get_endpoint_status(end)
                if len(statuses) == 0:
                    logger.warn('Endpoint {} does not have any statuses'
                                .format(endpoint_name(end)))
                else:
                    status = statuses[0]  # Most recent endpoint status

                    # Mark endpoint as dead/alive based on heartbeat's age
                    # Heartbeats are delayed when an endpoint is executing
                    # tasks, so take into account last execution too
                    age = time.time() - max(status['timestamp'],
                                            self.last_result_time[end])
                    is_dead = end in self._dead_endpoints
                    if not is_dead and age > HEARTBEAT_THRESHOLD:
                        self._dead_endpoints.add(end)
                        logger.warn('Endpoint {} seems to have died! '
                                    'Last heartbeat was {:.2f} seconds ago.'
                                    .format(endpoint_name(end), age))
                    elif is_dead and age <= HEARTBEAT_THRESHOLD:
                        self._dead_endpoints.remove(end)
                        logger.warn('Endpoint {} is back alive! '
                                    'Last heartbeat was {:.2f} seconds ago.'
                                    .format(endpoint_name(end), age))

                    # Mark endpoint as "cold" or "warm" depending on if it
                    # has active managers (nodes) allocated to it
                    if self.temperature[end] == 'WARM' \
                            and status['active_managers'] == 0:
                        self.temperature[end] = 'COLD'
                        logger.info('Endpoint {} is cold!'
                                    .format(endpoint_name(end)))
                    elif self.temperature[end] != 'WARM' \
                            and status['active_managers'] > 0:
                        self.temperature[end] = 'WARM'
                        logger.info('Endpoint {} is warm again!'
                                    .format(endpoint_name(end)))

            # Send backup tasks if needed
            self._send_backups_if_needed()

            # Sleep before checking statuses again
            time.sleep(5)

    def _send_backups_if_needed(self):
        # Get all tasks which have not been completed yet and still have a
        # pending (real) task on a dead endpoint
        task_ids = {
            self._pending[real_task_id]['task_id']
            for endpoint in self._dead_endpoints
            for real_task_id in self._pending_by_endpoint[endpoint]
            if self._pending[real_task_id]['task_id'] in self._task_info
        }

        # Get all tasks for which we had ETA-predictions but haven't
        # been completed even past their ETA
        for real_task_id, info in self._pending.items():
            # If the predicted ETA wasn't reliable, don't send backups
            if not info['is_ETA_reliable']:
                continue

            expected = info['ETA'] - info['time_sent']
            elapsed = time.time() - info['time_sent']

            if elapsed / expected > self.backup_delay_threshold:
                task_ids.add(info['task_id'])

        for task_id in task_ids:
            if len(self._endpoints_sent_to[task_id]) > self.max_backups:
                logger.debug(f'Skipping sending new backup task for {task_id}')
            else:
                logger.info(f'Sending new backup task for {task_id}')
                info = self._task_info[task_id]
                self._schedule_task(info['function_id'], info['payload'],
                                    info['headers'], info['files'], task_id)
