import asyncio
import logging
import struct
from enum import Enum
from math import ceil
from typing import Optional, Tuple, List, cast

import contexttimer
from bitarray import bitarray

from file_structure import FileStructure
from models import DownloadInfo, Peer, SHA1_DIGEST_LEN, BlockRequest

CLIENT_LOGGER_LEVEL = logging.INFO

TIMER_WARNING_THRESHOLD_MS = 50


class MessageType(Enum):
    choke = 0
    unchoke = 1
    interested = 2
    not_interested = 3
    have = 4
    bitfield = 5
    request = 6
    piece = 7
    cancel = 8
    port = 9


class PeerTCPClient:
    def __init__(self, download_info: DownloadInfo, file_structure: FileStructure, our_peer_id: bytes, peer: Peer):
        self._download_info = download_info
        self._file_structure = file_structure
        self._our_peer_id = our_peer_id
        self._peer = peer

        self._logger = logging.getLogger('[{}]'.format(peer))
        self._logger.setLevel(CLIENT_LOGGER_LEVEL)

        self._am_choking = True
        self._am_interested = False
        self._peer_choking = True
        self._peer_interested = False

        self._downloaded = 0
        self._uploaded = 0
        self._distrust_rate = 0

        self._reader = None  # type: asyncio.StreamReader
        self._writer = None  # type: asyncio.StreamWriter
        self._connected = False

    PEER_HANDSHAKE_MESSAGE = b'BitTorrent protocol'

    CONNECT_TIMEOUT = 3
    READ_TIMEOUT = 3
    MAX_SILENCE_DURATION = 5 * 60
    WRITE_TIMEOUT = 3

    async def _perform_handshake(self):
        info_hash = self._download_info.info_hash

        message = PeerTCPClient.PEER_HANDSHAKE_MESSAGE
        message_len = len(message)
        handshake_data = (bytes([message_len]) + message + b'\0' * 8 +
                          info_hash + self._our_peer_id)
        self._writer.write(handshake_data)
        self._logger.debug('handshake sent')

        response = await asyncio.wait_for(self._reader.readexactly(len(handshake_data)), PeerTCPClient.READ_TIMEOUT)

        if response[:message_len + 1] != handshake_data[:message_len + 1]:
            raise ValueError('Unknown protocol')
        offset = message_len + 1 + 8

        if response[offset:offset + SHA1_DIGEST_LEN] != info_hash:
            raise ValueError("info_hashes don't match")
        offset += SHA1_DIGEST_LEN

        actual_peer_id = response[offset:offset + len(self._our_peer_id)]
        if self._our_peer_id == actual_peer_id:
            raise ValueError('Connection to ourselves')
        if self._peer.peer_id is not None and self._peer.peer_id != actual_peer_id:
            raise ValueError('Unexpected peer_id')
        self._peer.peer_id = actual_peer_id

        self._logger.debug('handshake performed')

    async def connect(self):
        self._logger.debug('trying to connect')

        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._peer.host, self._peer.port), PeerTCPClient.CONNECT_TIMEOUT)
        self._logger.debug('connected')

        try:
            await self._perform_handshake()
        except:
            self.close()
            raise

        self._connected = True

    @property
    def am_choking(self):
        return self._am_choking

    @property
    def am_interested(self):
        return self._am_interested

    def _check_connect(self):
        if not self._connected:
            raise RuntimeError("Can't change state when the client isn't connected")

    @am_choking.setter
    def am_choking(self, value: bool):
        self._check_connect()
        if self._am_choking != value:
            self._am_choking = value
            self._send_message(MessageType.choke if value else MessageType.unchoke)

    @am_interested.setter
    def am_interested(self, value: bool):
        self._check_connect()
        if self._am_interested != value:
            self._am_interested = value
            self._send_message(MessageType.interested if value else MessageType.not_interested)

    @property
    def peer_choking(self):
        return self._peer_choking

    @property
    def peer_interested(self):
        return self._peer_interested

    @property
    def downloaded(self):
        return self._downloaded

    @property
    def uploaded(self):
        return self._uploaded

    @property
    def distrust_rate(self):
        return self._distrust_rate

    def increase_distrust(self):
        self._distrust_rate += 1

    async def _receive_message(self) -> Optional[Tuple[MessageType, memoryview]]:
        data = await asyncio.wait_for(self._reader.readexactly(4), PeerTCPClient.MAX_SILENCE_DURATION)
        (length,) = struct.unpack('!I', data)
        if length == 0:  # keep-alive
            return None

        # FIXME: Don't receive too much stuff
        data = await asyncio.wait_for(self._reader.readexactly(length), PeerTCPClient.READ_TIMEOUT)
        try:
            message_id = MessageType(data[0])
        except ValueError:
            self._logger.debug('Unknown message type %s', data[0])
            return None
        payload = memoryview(data)[1:]

        self._logger.debug('incoming message %s length=%s', message_id.name, length)

        return message_id, payload

    def _send_message(self, message_id: MessageType=None, *payload: List[bytes]):
        if message_id is None:  # keep-alive
            self._writer.write('\0' * 4)

        length = sum(len(portion) for portion in payload) + 1

        self._logger.debug('outcoming message %s length=%s', message_id.name, length)

        self._writer.write(struct.pack('!IB', length, message_id.value))
        for portion in payload:
            self._writer.write(portion)

    @staticmethod
    def _check_payload_len(message_id: MessageType, payload: memoryview, expected_len: int):
        if len(payload) != expected_len:
            raise ValueError('Invalid payload length on message_id = {} '
                             '(expected {}, got {})'.format(message_id.name, expected_len, len(payload)))

    def _handle_setting_states(self, message_id: MessageType, payload: memoryview):
        PeerTCPClient._check_payload_len(message_id, payload, 0)

        if message_id == MessageType.choke:
            self._peer_choking = True
        elif message_id == MessageType.unchoke:
            self._peer_choking = False
        elif message_id == MessageType.interested:
            self._peer_interested = True
        elif message_id == MessageType.not_interested:
            self._peer_interested = False

    def _mark_as_owner(self, piece_index: int):
        self._download_info.piece_owners[piece_index].add(self._peer)
        if piece_index in self._download_info.interesting_pieces:
            self.am_interested = True

    def _handle_haves(self, message_id: MessageType, payload: memoryview):
        if message_id == MessageType.have:
            (index,) = struct.unpack('!I', cast(bytes, payload))
            self._mark_as_owner(index)
        elif message_id == MessageType.bitfield:
            piece_count = self._download_info.piece_count
            PeerTCPClient._check_payload_len(message_id, payload, int(ceil(piece_count / 8)))

            arr = bitarray(endian='big')
            arr.frombytes(payload.tobytes())
            for i in range(piece_count):
                if arr[i]:
                    self._mark_as_owner(i)
            for i in range(piece_count, len(arr)):
                if arr[i]:
                    raise ValueError('Spare bits in "bitfield" message must be zero')

    MAX_REQUEST_LENGTH = 2 ** 17

    def _check_position_range(self, request: BlockRequest):
        if request.piece_index < 0 or request.piece_index >= self._download_info.piece_count:
            raise IndexError('Piece index out of range')
        end_offset = request.piece_index * self._download_info.piece_length + \
            request.block_begin + request.block_length
        if (request.block_begin < 0 or request.block_begin + request.block_length > self._download_info.piece_length or
                end_offset > self._download_info.total_size):
            raise IndexError('Position in piece out of range')

    def _send_block(self, request: BlockRequest):
        block = self._file_structure.read(
            request.piece_index * self._download_info.piece_length + request.block_begin, request.block_length)

        self._send_message(MessageType.piece, struct.pack('!2I', request.piece_index, request.block_begin), block)

        self._uploaded += request.block_length
        self._download_info.total_uploaded += request.block_length

    async def _process_requests(self, message_id: MessageType, payload: memoryview):
        piece_index, begin, length = struct.unpack('!3I', cast(bytes, payload))
        request = BlockRequest(piece_index, begin, length, None)
        self._check_position_range(request)

        if message_id == MessageType.request:
            if length > PeerTCPClient.MAX_REQUEST_LENGTH:
                raise ValueError('Requested {} bytes, but the current policy allows to accept requests '
                                 'of not more than {} bytes'.format(length, PeerTCPClient.MAX_REQUEST_LENGTH))
            if (self._am_choking or not self._peer_interested or
                    not self._download_info.piece_downloaded[piece_index]):
                # If peer isn't interested but requesting, their peer_interested flag wasn't considered
                # when selecting who to unchoke, so we may be not ready to upload to them.
                # If requested piece is not downloaded yet, we shouldn't disconnect because our piece_downloaded flag
                # could be removed because of file corruption.
                return

            # FIXME: Check here if block hasn't been cancelled. We need sure that cancel message can be received
            self._send_block(request)
            await self.drain()
            self._logger.info('block sent')
        elif message_id == MessageType.cancel:
            pass

    def _handle_block(self, payload: memoryview):
        if not self._am_interested:
            # For example, we can be not interested in pieces from peers with big distrust rate
            return

        fmt = '!2I'
        piece_index, block_begin = struct.unpack_from(fmt, payload)
        block_data = memoryview(payload)[struct.calcsize(fmt):]
        block_length = len(block_data)
        request = BlockRequest(piece_index, block_begin, block_length, None)
        self._check_position_range(request)

        if self._download_info.piece_downloaded[piece_index] or not block_length:
            return

        self._downloaded += block_length
        self._download_info.total_downloaded += block_length

        with contexttimer.Timer() as timer:
            self._file_structure.write(piece_index * self._download_info.piece_length + block_begin, block_data)

            self._download_info.mark_downloaded_blocks(request)
            self._download_info.piece_sources[piece_index].add(self._peer)
        if timer.elapsed >= TIMER_WARNING_THRESHOLD_MS:
            self._logger.warning('Too long _handle_block (%s ms)', timer.elapsed)

    async def run(self):
        while True:
            message = await self._receive_message()
            if message is None:
                continue
            message_id, payload = message
            # FIXME: send keep-alives (or do it in another Task)

            if message_id in (MessageType.choke, MessageType.unchoke,
                              MessageType.interested, MessageType.not_interested):
                self._handle_setting_states(message_id, payload)
            elif message_id in (MessageType.have, MessageType.bitfield):
                self._handle_haves(message_id, payload)
            elif message_id in (MessageType.request, MessageType.cancel):
                await self._process_requests(message_id, payload)
            elif message_id == MessageType.piece:
                self._handle_block(payload)
            elif message_id == MessageType.port:
                PeerTCPClient._check_payload_len(message_id, payload, 2)
                # TODO: Ignore or implement DHT

    def send_have(self, piece_index: int):
        self._send_message(MessageType.have, struct.pack('!I', piece_index))

    def send_request(self, request: BlockRequest):
        self._check_position_range(request)
        assert self._peer in self._download_info.piece_owners[request.piece_index]

        self._send_message(MessageType.request,
                           struct.pack('!3I', request.piece_index, request.block_begin, request.block_length))

    async def drain(self):
        await asyncio.wait_for(self._writer.drain(), PeerTCPClient.WRITE_TIMEOUT)

    def close(self):
        self._writer.close()

        self._connected = False
