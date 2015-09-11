# Copyright (c) 2015 Nicolas JOUANIN
#
# See the file license.txt for copying permission.
from asyncio import futures
from hbmqtt.mqtt.protocol.handler import ProtocolHandler
from hbmqtt.mqtt.packet import *
from hbmqtt.mqtt.disconnect import DisconnectPacket
from hbmqtt.mqtt.pingreq import PingReqPacket
from hbmqtt.mqtt.pingresp import PingRespPacket
from hbmqtt.mqtt.subscribe import SubscribePacket
from hbmqtt.mqtt.suback import SubackPacket
from hbmqtt.mqtt.unsubscribe import UnsubscribePacket
from hbmqtt.mqtt.unsuback import UnsubackPacket
from hbmqtt.session import Session
from hbmqtt.plugins.manager import PluginManager


class ClientProtocolHandler(ProtocolHandler):
    def __init__(self, session: Session, plugins_manager: PluginManager, loop=None):
        super().__init__(session, plugins_manager, loop=loop)
        self._ping_task = None
        self._pingresp_queue = asyncio.Queue(loop=self._loop)
        self._subscriptions_waiter = dict()
        self._unsubscriptions_waiter = dict()
        self._disconnect_waiter = None
        self._pingresp_waiter = None

    @asyncio.coroutine
    def start(self):
        yield from super().start()
        if self._disconnect_waiter is None:
            self._disconnect_waiter = futures.Future(loop=self._loop)

    @asyncio.coroutine
    def stop(self):
        yield from super().stop()
        if self._ping_task:
            try:
                self._ping_task.cancel()
            except Exception:
                pass
        if self._pingresp_waiter:
            self._pingresp_waiter.cancel()

    def handle_write_timeout(self):
        self._ping_task = self._loop.call_soon(asyncio.async, self.mqtt_ping())

    def handle_read_timeout(self):
        pass

    @asyncio.coroutine
    def mqtt_subscribe(self, topics, packet_id):
        """
        :param topics: array of topics [{'filter':'/a/b', 'qos': 0x00}, ...]
        :return:
        """

        # Build and send SUBSCRIBE message
        subscribe = SubscribePacket.build(topics, packet_id)
        yield from self._send_packet(subscribe)

        # Wait for SUBACK is received
        waiter = futures.Future(loop=self._loop)
        self._subscriptions_waiter[subscribe.variable_header.packet_id] = waiter
        return_codes = yield from waiter

        del self._subscriptions_waiter[subscribe.variable_header.packet_id]
        return return_codes

    @asyncio.coroutine
    def handle_suback(self, suback: SubackPacket):
        packet_id = suback.variable_header.packet_id
        try:
            waiter = self._subscriptions_waiter.get(packet_id)
            waiter.set_result(suback.payload.return_codes)
        except KeyError as ke:
            self.logger.warn("Received SUBACK for unknown pending subscription with Id: %s" % packet_id)

    @asyncio.coroutine
    def mqtt_unsubscribe(self, topics, packet_id):
        """

        :param topics: array of topics ['/a/b', ...]
        :return:
        """
        unsubscribe = UnsubscribePacket.build(topics, packet_id)
        yield from self._send_packet(unsubscribe)
        waiter = futures.Future(loop=self._loop)
        self._unsubscriptions_waiter[unsubscribe.variable_header.packet_id] = waiter
        yield from waiter
        del self._unsubscriptions_waiter[unsubscribe.variable_header.packet_id]

    @asyncio.coroutine
    def handle_unsuback(self, unsuback: UnsubackPacket):
        packet_id = unsuback.variable_header.packet_id
        try:
            waiter = self._unsubscriptions_waiter.get(packet_id)
            waiter.set_result(None)
        except KeyError as ke:
            self.logger.warn("Received UNSUBACK for unknown pending subscription with Id: %s" % packet_id)

    @asyncio.coroutine
    def mqtt_disconnect(self):
        disconnect_packet = DisconnectPacket()
        yield from self._send_packet(disconnect_packet)
        self._connack_waiter = None

    @asyncio.coroutine
    def mqtt_ping(self):
        ping_packet = PingReqPacket()
        yield from self._send_packet(ping_packet)
        self._pingresp_waiter = futures.Future(loop=self._loop)
        resp = yield from self._pingresp_queue.get()
        self._pingresp_waiter = None
        return resp

    @asyncio.coroutine
    def handle_pingresp(self, pingresp: PingRespPacket):
        yield from self._pingresp_queue.put(pingresp)

    @asyncio.coroutine
    def handle_connection_closed(self):
        self.logger.debug("Broker closed connection")
        if not self._disconnect_waiter.done():
            self._disconnect_waiter.set_result(None)

    @asyncio.coroutine
    def wait_disconnect(self):
        yield from self._disconnect_waiter
