# Copyright (c) 2020 Seagate Technology LLC and/or its Affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# For any questions about this software or licensing,
# please email opensource@seagate.com or cortx-questions@seagate.com.
#

import logging
import time
from queue import Empty, Queue
from typing import List

from hax.message import (BroadcastHAStates, FirstEntrypointRequest,
                         EntrypointRequest, HaNvecGetEvent,
                         ProcessEvent, SnsRebalancePause, SnsRebalanceResume,
                         SnsRebalanceStart, SnsRebalanceStatus,
                         SnsRebalanceStop, SnsRepairPause, SnsRepairResume,
                         SnsRepairStart, SnsRepairStatus, SnsRepairStop)
from hax.motr import Motr
from hax.motr.delivery import DeliveryHerald
from hax.queue.publish import EQPublisher
from hax.types import (ConfHaProcess, HAState, MessageId, StobIoqError,
                       m0HaProcessEvent, m0HaProcessType, ServiceHealth,
                       StoppableThread, HaLinkMessagePromise, ObjT)
from hax.util import ConsulUtil, dump_json, repeat_if_fails

LOG = logging.getLogger('hax')


class ConsumerThread(StoppableThread):
    """
    The only Motr-aware thread in whole HaX. This thread pulls messages from
    the multithreaded Queue and considers the messages as commands. Every such
    a command describes what should be sent to Motr land.

    The thread exits gracefully when it receives message of type Die (i.e.
    it is a 'poison pill').
    """
    def __init__(self, q: Queue, motr: Motr, herald: DeliveryHerald,
                 consul: ConsulUtil):
        super().__init__(target=self._do_work,
                         name='qconsumer',
                         args=(q, motr))
        self.is_stopped = False
        self.consul = consul
        self.eq_publisher = EQPublisher()
        self.herald = herald

    def stop(self) -> None:
        self.is_stopped = True

    @repeat_if_fails(wait_seconds=1)
    def _update_process_status(self, q: Queue, event: ConfHaProcess) -> None:
        # If a consul-related exception appears, it will
        # be processed by repeat_if_fails.
        #
        # This thread will become blocked until that
        # intermittent error gets resolved.
        self.consul.update_process_status(event)
        svc_status = m0HaProcessEvent.event_to_svchealth(event.chp_event)
        if event.chp_type == m0HaProcessType.M0_CONF_HA_PROCESS_M0D:
            # Broadcast the received motr process status to other motr
            # processes in the cluster.
            q.put(BroadcastHAStates(states=[HAState(fid=event.fid,
                                    status=svc_status)],
                  reply_to=None))

    @repeat_if_fails(wait_seconds=1)
    def update_process_failure(self, q: Queue,
                               ha_states: List[HAState]) -> List[HAState]:
        new_ha_states: List[HAState] = []
        for state in ha_states:
            # We are only concerned with process statuses.
            if state.fid.container == ObjT.PROCESS.value:
                current_status = self.consul.get_process_current_status(
                                     state.status, state.fid)
                if current_status == ServiceHealth.FAILED:
                    self.consul.service_health_to_m0dstatus_update(
                        state.fid, current_status)
                elif current_status == ServiceHealth.UNKNOWN:
                    # We got service status as UNKNOWN, that means hax was
                    # notified about process failure but hax couldn't
                    # confirm if the process is in failed state or have
                    # failed and restarted. So, we will not loose the
                    # event and try again to confirm the real time
                    # process status by enqueing a broadcast event
                    # specific to this process.
                    # It is expected that the process status gets
                    # eventually confirmed as either failed or passing (OK).
                    # This situation typically arises due to delay
                    # in receiving failure notification during which the
                    # corresponding process might be restarting or have
                    # already restarted. Thus it is important to confirm
                    # the real time status of the process before
                    # broadcasting failure.
                    current_status = ServiceHealth.OK
                    q.put(BroadcastHAStates(
                          states=[HAState(fid=state.fid,
                                          status=ServiceHealth.FAILED)],
                          reply_to=None))
                new_ha_states.append(HAState(fid=state.fid,
                                             status=current_status))
            else:
                new_ha_states.append(state)
        return new_ha_states

    def _do_work(self, q: Queue, motr: Motr):
        ffi = motr._ffi
        LOG.info('Handler thread has started')
        ffi.adopt_motr_thread()

        def pull_msg():
            try:
                return q.get(block=False)
            except Empty:
                return None

        try:
            while True:
                try:
                    LOG.debug('Waiting for the next message')

                    item = pull_msg()
                    while item is None:
                        time.sleep(0.2)
                        if self.is_stopped:
                            raise StopIteration()
                        item = pull_msg()

                    LOG.debug('Got %s message from queue', item)
                    if isinstance(item, FirstEntrypointRequest):
                        LOG.debug('first entrypoint request, broadcast FAILED')
                        ids: List[MessageId] = motr.broadcast_ha_states(
                            [HAState(fid=item.process_fid,
                                     status=ServiceHealth.FAILED)])
                        LOG.debug('waiting for broadcast of %s for ep: %s',
                                  ids, item.remote_rpc_endpoint)
                        self.herald.wait_for_all(HaLinkMessagePromise(ids))
                        motr.send_entrypoint_request_reply(
                            EntrypointRequest(
                                reply_context=item.reply_context,
                                req_id=item.req_id,
                                remote_rpc_endpoint=item.remote_rpc_endpoint,
                                process_fid=item.process_fid,
                                git_rev=item.git_rev,
                                pid=item.pid,
                                is_first_request=item.is_first_request))
                    elif isinstance(item, EntrypointRequest):
                        # While replying any Exception is catched. In such a
                        # case, the motr process will receive EAGAIN and
                        # hence will need to make new attempt by itself
                        motr.send_entrypoint_request_reply(item)
                    elif isinstance(item, ProcessEvent):
                        self._update_process_status(q, item.evt)
                    elif isinstance(item, HaNvecGetEvent):
                        fn = motr.ha_nvec_get_reply
                        # If a consul-related exception appears, it will
                        # be processed by repeat_if_fails.
                        #
                        # This thread will become blocked until that
                        # intermittent error gets resolved.
                        decorated = (repeat_if_fails(wait_seconds=5))(fn)
                        decorated(item)
                    elif isinstance(item, BroadcastHAStates):
                        LOG.info('HA states: %s', item.states)
                        ha_states = self.update_process_failure(q, item.states)
                        result: List[MessageId] = motr.broadcast_ha_states(
                            ha_states)
                        if item.reply_to:
                            item.reply_to.put(result)
                    elif isinstance(item, StobIoqError):
                        LOG.info('Stob IOQ: %s', item.fid)
                        payload = dump_json(item)
                        LOG.debug('Stob IOQ JSON: %s', payload)
                        offset = self.eq_publisher.publish('stob-ioq', payload)
                        LOG.debug('Written to epoch: %s', offset)
                    elif isinstance(item, SnsRepairStatus):
                        LOG.info('Requesting SNS repair status')
                        status = motr.get_repair_status(item.fid)
                        LOG.info('SNS repair status is received: %s', status)
                        item.reply_to.put(status)
                    elif isinstance(item, SnsRebalanceStatus):
                        LOG.info('Requesting SNS rebalance status')
                        status = motr.get_rebalance_status(item.fid)
                        LOG.info('SNS rebalance status is received: %s',
                                 status)
                        item.reply_to.put(status)
                    elif isinstance(item, SnsRebalanceStart):
                        LOG.info('Requesting SNS rebalance start')
                        motr.start_rebalance(item.fid)
                    elif isinstance(item, SnsRebalanceStop):
                        LOG.info('Requesting SNS rebalance stop')
                        motr.stop_rebalance(item.fid)
                    elif isinstance(item, SnsRebalancePause):
                        LOG.info('Requesting SNS rebalance pause')
                        motr.pause_rebalance(item.fid)
                    elif isinstance(item, SnsRebalanceResume):
                        LOG.info('Requesting SNS rebalance resume')
                        motr.resume_rebalance(item.fid)
                    elif isinstance(item, SnsRepairStart):
                        LOG.info('Requesting SNS repair start')
                        motr.start_repair(item.fid)
                    elif isinstance(item, SnsRepairStop):
                        LOG.info('Requesting SNS repair stop')
                        motr.stop_repair(item.fid)
                    elif isinstance(item, SnsRepairPause):
                        LOG.info('Requesting SNS repair pause')
                        motr.pause_repair(item.fid)
                    elif isinstance(item, SnsRepairResume):
                        LOG.info('Requesting SNS repair resume')
                        motr.resume_repair(item.fid)

                    else:
                        LOG.warning('Unsupported event type received: %s',
                                    item)
                except StopIteration:
                    raise
                except Exception:
                    # no op, swallow the exception
                    LOG.exception('**ERROR**')
        except StopIteration:
            ffi.shun_motr_thread()
        finally:
            LOG.info('Handler thread has exited')
