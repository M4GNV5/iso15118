"""
This module contains the SessionStateMachine and the V2GCommunicationSession,
which itself is subclassing the SessionStateMachine. These classes can be used
by both EVCC and SECC, as they share the exact same functionality when sending,
receiving, and processing messages during an ISO 15118 communication session.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from asyncio.streams import StreamReader, StreamWriter
from typing import List, Optional, Tuple, Type, Union

from pydantic import ValidationError
from typing_extensions import TYPE_CHECKING

from iso15118.shared.exceptions import (
    EXIDecodingError,
    FaultyStateImplementationError,
    InvalidV2GTPMessageError,
    MessageProcessingError,
)
from iso15118.shared.exi_codec import from_exi
from iso15118.shared.messages.app_protocol import (
    SupportedAppProtocolReq,
    SupportedAppProtocolRes,
)
from iso15118.shared.messages.enums import Namespace, Protocol
from iso15118.shared.messages.iso15118_2.datatypes import EnergyTransferModeEnum
from iso15118.shared.messages.iso15118_2.datatypes import (
    SelectedService as SelectedServiceV2,
)
from iso15118.shared.messages.iso15118_2.msgdef import V2GMessage as V2GMessageV2
from iso15118.shared.messages.iso15118_20.common_types import (
    V2GMessage as V2GMessageV20,
)
from iso15118.shared.messages.sdp import Security
from iso15118.shared.messages.v2gtp import V2GTPMessage
from iso15118.shared.notifications import StopNotification
from iso15118.shared.states import Pause, State, Terminate
from iso15118.shared.utils import wait_till_finished

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # EVCCCommunicationSession and SECCCommunicationSession are used for
    # annotation purposes only, as a type hint for the comm_session class
    # attribute. But comm_session also imports State. To avoid a circular import
    # error, one can use the TYPE_CHECKING boolean from typing, which evaluates
    # to True during mypy or other 3rd party type checker but assumes the value
    # 'False' during runtime.
    # Please check:
    # https://stackoverflow.com/questions/61545580/how-does-mypy-use-typing-type-checking-to-resolve-the-circular-import-annotation
    # https://docs.python.org/3/library/typing.html#typing.TYPE_CHECKING
    from iso15118.evcc.comm_session_handler import EVCCCommunicationSession
    from iso15118.secc.comm_session_handler import SECCCommunicationSession


class SessionStateMachine(ABC):
    """
    Each newly established TCP session initiates a communication session, which
    is essentially the sate machine for the ISO 15118 message handling.
    """

    def __init__(
        self,
        start_state: Type[State],
        comm_session: Union["EVCCCommunicationSession", "SECCCommunicationSession"],
    ):
        """
        The EVCC state machine starts with waiting for the
        SupportedAppProtocolRes message from the SECC.

        The SECC state machine starts with waiting for the
        SupportedAppProtocolReq message from the EVCC.

        The ISO 15118 version is determined with the SupportedAppProtocolRequest.
        The states ProcessSupportedAppProtocolReq (SECC) and
        ProcessSupportedAppProtocolRes (EVCC), respectively, need to set the
        iso_version of the corresponding CommunicationSession object.

        Args:
            start_state: The state that initialises the state machine
            comm_session:   The V2GCommunicationSession object of either the
                            the EVCC or SECC. Needed to access certain session
                            variables, as certain states need to read
                            and store session relevant information, depending on
                            the message.

                            For example: the SupportedAppProtocolReq message
                            contains information about the EVCC's supported
                            protocol, which is relevant information needed
                            throughout the session.
        """
        self.start_state = start_state
        self.comm_session = comm_session
        self.current_state = start_state(comm_session)

    def get_exi_ns(self) -> str:
        """
        Provides the right protocol namespace for the EXI decoder.
        In DIN SPEC 70121 and ISO 15118-2, all messages are defined
        in one XSD schema (e.g. 'urn:iso:15118:2:2013:MsgDef' for ISO 15118-2),
        but in ISO 15118-20, we need to distinguish between Common, AC, DC, WPT,
        and ACDP.

        An ISO 15118-20 communication starts with messages defined in namespace
        'urn:iso:std:iso:15118:-20:CommonMessages', but some messages are then
        energy mode specific and, thus, we need the specific schema where these
        messages are defined.
        """
        if self.comm_session.protocol == Protocol.UNKNOWN:
            return Namespace.SAP
        elif self.comm_session.protocol == Protocol.ISO_15118_2:
            return Namespace.ISO_V2_MSG_DEF
        elif self.comm_session.protocol == Protocol.DIN_SPEC_70121:
            return Namespace.DIN_MSG_BODY
        elif str(self.current_state).startswith("AC"):
            return Namespace.ISO_V20_AC
        elif str(self.current_state).startswith("DC"):
            return Namespace.ISO_V20_DC
        elif str(self.current_state).startswith("WPT"):
            return Namespace.ISO_V20_WPT
        elif str(self.current_state).startswith("ACDP"):
            return Namespace.ISO_V20_ACDP
        else:
            return Namespace.ISO_V20_COMMON_MSG

    def process_message(self, message: bytes):
        """
        The following steps are conducted in this state machine's general
        process_message() function:

        1. Try to create a V2GTP (V2G Transfer Protocol) message from the
           incoming byte stream.
        2. If step 1 didn't raise a InvalidV2GTPMessageError then we try to
           EXI decode the V2GTP payload
        3. If step 2 didn't raise an EXIDecodingError then we hand over the
           decoded payload to the current state's process_message() function,
           which will create the next message to send - but only in case a new
           message needs to be sent. For that to be true, the next_v2gtp_msg
           must have been set by the current state, which is usually the case
           for the EVCC if the next state is not a Terminate state. The SECC
           will always send a next response, even if the next state is Terminate.
           The next state to transition to is also determined by the state's
           process_message() method.

        Args:
            message:    The incoming message from the EVCC/SECC, given as a
                        bytearray (the payload of a V2GTPMessage).
                        The message can be a
                        - SupportedAppProtocolRequest  (EVCC),
                        - SupportedAppProtocolResponse (SECC),
                        - V2GMessage according to the DIN SPEC 70121 standard,
                        - V2GMessage according to the ISO 15118-2 standard, or
                        - V2GMessage according to the ISO 15118-20 standard

        Raises:
            MessageProcessingError, FaultyStateImplementationError,
            EXIDecodingError
        """
        # Step 1
        try:
            # First extract the V2GMessage payload from the V2GTPMessage ...
            v2gtp_msg = V2GTPMessage.from_bytes(self.comm_session.protocol, message)
        except InvalidV2GTPMessageError as exc:
            logger.exception("Incoming TCPPacket is not a valid V2GTPMessage")
            # and then decode the bytearray into the message
            raise MessageProcessingError from exc

        # Step 2
        decoded_message: Union[
            SupportedAppProtocolReq,
            SupportedAppProtocolRes,
            V2GMessageV2,
            V2GMessageV20,
            None,
        ] = None
        try:
            decoded_message = from_exi(v2gtp_msg.payload, self.get_exi_ns())
        except EXIDecodingError as exc:
            logger.error(f"{exc.error}")
            self.current_state.next_state = Terminate
            raise exc

        # Shouldn't happen, but just to be sure (otherwise mypy would complain)
        if not decoded_message:
            logger.error(
                "Unusual error situation: decoded_message is None"
                "although no EXIDecodingError was raised"
            )
            return

        # Step 3
        try:
            logger.debug(f"{str(decoded_message)} received")
            self.current_state.process_message(decoded_message)
        except MessageProcessingError as exc:
            logger.exception(
                f"{exc.__class__.__name__} while processing " f"{exc.message_name}"
            )
            raise exc
        except FaultyStateImplementationError as exc:
            logger.exception(f"{exc.__class__.__name__}: {exc.error}")
            raise exc
        except ValidationError as exc:
            logger.exception(f"{exc.__class__.__name__}: {exc.raw_errors}")
            raise exc
        except AttributeError as exc:
            logger.exception(f"{exc}")
            raise exc

        if (
            self.current_state.next_v2gtp_msg is None
            and self.current_state.next_state is not Terminate
        ):
            raise FaultyStateImplementationError(
                "Field 'next_v2gtp_msg' is "
                "None but must be set because "
                "next state is not Terminate"
            )

    def go_to_next_state(self):
        """
        This method assures that the communication session's current state is
        always up-to-date, which is something other parts of the code rely on.

        Is automatically called by the rcv_loop after sending the next message.
        But we only transition if the current state defined a next state.
        If that's not the case, the current state will decide how to transition
        based on the next incoming message.
        """
        if self.current_state.next_state:
            self.current_state.next_state(self.comm_session)

    def resume(self):
        logger.debug("Trying to resume communication session")
        self.current_state = self.start_state(self.comm_session)


class V2GCommunicationSession(SessionStateMachine):
    """
    A communication session class that is shared between the EVCC and the SECC
    to execute the corresponding state machine, process incoming messages, and
    send subsequent messages as a result.
    """

    # pylint: disable=too-many-instance-attributes

    def __init__(
        self,
        transport: Tuple[StreamReader, StreamWriter],
        start_state: Type["State"],
        session_handler_queue: asyncio.Queue,
        comm_session: Union["EVCCCommunicationSession", "SECCCommunicationSession"],
    ):
        """
        Initialise the communication session with EVCC or SECC specific
        parameters

        Args:
            transport:  A tuple consisting of a StreamReader and StreamWriter
                        object for the TCP socket
            start_state: The state that initialises the state machine
            session_handler_queue:  The asyncio.Queue object used for pushing
                                    timeout, termination, and pausing
                                    notifications to the session handler
            is_tls: Whether or not this TCP communication session is cryptographically
                    secured with TLS
            comm_session: The instance of EVCCCommunicationSession or
                          SECCCommunicationSession
        """
        self.protocol: Protocol = Protocol.UNKNOWN
        self.reader, self.writer = transport
        # For timeout, termination, and pausing notifications
        self.session_handler_queue = session_handler_queue
        self.session_id: str = ""
        # Mutually agreed-upon ISO 15118 application protocol as result of SAP
        self.chosen_protocol: str = ""
        # The services offered by the SECC and selected by the EVCC
        self.selected_services: List[SelectedServiceV2] = []
        # Selected energy modes helps to choose AC or DC specific parameters
        self.selected_energy_mode: Optional[EnergyTransferModeEnum] = None
        # The SAScheduleTuple element the EVCC chose (referenced by ID)
        self.selected_schedule: Optional[int] = None
        # Contains info whether the communication session is stopped successfully (True)
        # or due to a failure (False), plus additional info regarding the reason behind.
        self.stop_reason: Optional[StopNotification] = None
        self.last_message_sent: Optional[V2GTPMessage] = None
        self._started: bool = True

        logger.debug("Starting a new communication session")
        SessionStateMachine.__init__(self, start_state, comm_session)

    async def start(self, timeout: float):
        """
        Starts a EVCC / SECC communication session by spawning up the rcv_loop()
        method, that constantly waits a given amount of seconds to read data
        from the TCP socket and process the incoming data.

        Args:
            timeout:    The time the EVCC / SECC is waiting for a next message
        """
        tasks = [self.rcv_loop(timeout)]

        try:
            self._started = True
            await wait_till_finished(tasks)
        finally:
            self._started = False

    @abstractmethod
    def save_session_info(self):
        raise NotImplementedError

    async def stop(self):
        """
        Closes the TCP connection after 5 seconds and terminates or pauses the
        data link for this V2GCommunicationSession object after 2 seconds to
        make sure any message that needs to be sent can still go through.
        TODO check if that really makes sense of if TCP should be terminated
             after 2 s and the data link after 5 s

        Especially necessary for the SECC, which needs to send a response with
        a FAILED response code or a SessionStopRes with response code "OK"
        before closing the TCP connection.

        Not a formal requirement in ISO 15118-2, but a best practice decided
        within the ISO 15118 User Group, and it became a formal requirement in
        ISO 15118-20 (at least for the SECC side, see [V2G20-1633]).
        """
        if self.current_state.next_state == Pause:
            self.save_session_info()
            terminate_or_pause = "Pause"
        else:
            terminate_or_pause = "Terminat"

        logger.debug(
            f"{terminate_or_pause}ing the data link in 2 seconds and "
            "closing the TCP connection in 5 seconds. "
            f"Reason: {self.stop_reason.reason}"
        )

        await asyncio.sleep(2)
        # TODO Signal data link layer to either terminate or pause the data
        #      link connection
        logger.debug(f"{terminate_or_pause}ed the data link")
        await asyncio.sleep(3)
        self.writer.close()
        await self.writer.wait_closed()
        logger.debug(
            "TCP connection closed to peer with address "
            f"{self.writer.get_extra_info('peername')}"
        )

    async def send(self, message: V2GTPMessage):
        """
        Sends a V2GTPMessage via the TCP socket and stores the last message sent

        Args:
            message: A V2GTPMessage
        """
        logger.debug(f"Sending {str(self.current_state.next_msg)}")
        # TODO: we may also check for writer exceptions
        self.writer.write(message.to_bytes())
        await self.writer.drain()
        self.last_message_sent = message

    async def rcv_loop(self, timeout: float):
        """
        A constant loop that implements the timeout for each message. Starts
        waiting for a specified time (see argument 'timeout') to read something
        from the TCP socket via the StreamReader. Once data is received, it is
        processed and the according next message will be sent. The processing of
        the incoming data also returns the timeout value for the next message
        being sent, and the loop starts over again waiting to read data on the
        TCP socket for the given amount of time ('timeoout').

        We expect the incoming message to be an EXI encoded message.

        Args:
            timeout:    The time the EVCC / SECC is waiting for a next message
        """
        while True:
            try:
                # The biggest message is the Certificate Installation Response,
                # which is estimated to be maximum between 5k to 6k
                # TODO check if that still holds with -20 (e.g. cross certs)
                message = await asyncio.wait_for(self.reader.read(7000), timeout)

                if message == b"" and self.reader.at_eof():
                    await self.stop()
                    self.session_handler_queue.put_nowait(
                        StopNotification(
                            False,
                            "TCP peer closed connection",
                            self.writer.get_extra_info("peername"),
                        )
                    )
                    return
            except asyncio.TimeoutError as exc:
                if self.last_message_sent:
                    error_msg = (
                        f"{exc.__class__.__name__} occurred. Waited "
                        f"for {timeout} s after sending last message: "
                        f"{str(self.last_message_sent)}"
                    )
                else:
                    error_msg = (
                        f"{exc.__class__.__name__} occurred. Waited "
                        f"for {timeout} s. No V2GTP message was "
                        "previously sent. This is probably a timeout "
                        f"while waiting for SupportedAppProtocolReq"
                    )

                self.stop_reason = StopNotification(
                    False, error_msg, self.writer.get_extra_info("peername")
                )

                await self.stop()
                self.session_handler_queue.put_nowait(self.stop_reason)
                return

            try:
                # This will create the values needed for the next state, such as
                # next_state, next_v2gtp_message, next_message_payload_type etc.
                self.process_message(message)

                if self.current_state.next_v2gtp_msg:
                    # next_v2gtp_msg would not be set only if the next state is either
                    # Terminate or Pause on the EVCC side
                    await self.send(self.current_state.next_v2gtp_msg)

                if self.current_state.next_state in (Terminate, Pause):
                    await self.stop()
                    self.comm_session.session_handler_queue.put_nowait(
                        self.comm_session.stop_reason
                    )
                    return

                timeout = self.current_state.next_msg_timeout
                self.go_to_next_state()
            except (
                MessageProcessingError,
                FaultyStateImplementationError,
                EXIDecodingError,
            ) as exc:
                message_name = ""
                additional_info = ""
                if isinstance(exc, MessageProcessingError):
                    message_name = exc.message_name
                if isinstance(exc, FaultyStateImplementationError):
                    additional_info = ": " + exc.error
                if isinstance(exc, EXIDecodingError):
                    additional_info = ": " + exc.__str__()

                self.stop_reason = StopNotification(
                    False,
                    f"{exc.__class__.__name__} occurred while processing message "
                    f"{message_name} in state {str(self.current_state)}{additional_info}",
                    self.writer.get_extra_info("peername"),
                )

                await self.stop()
                self.session_handler_queue.put_nowait(self.stop_reason)
                return
            except (AttributeError, ValueError) as exc:
                self.stop_reason = StopNotification(
                    False,
                    f"{exc.__class__.__name__} occurred while processing message in "
                    f"state {str(self.current_state)}: {exc}",
                    self.writer.get_extra_info("peername"),
                )

                await self.stop()
                self.session_handler_queue.put_nowait(self.stop_reason)
                return