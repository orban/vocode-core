from __future__ import annotations

import asyncio
import audioop
import io
import os
import queue
import random
import re
import threading
import time
import typing
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Tuple,
    Union,
)

import sentry_sdk
from fuzzywuzzy import fuzz
from loguru import logger
from pydub import AudioSegment
from sentry_sdk.tracing import Span

from vocode import conversation_id as ctx_conversation_id
from vocode.streaming.action.worker import ActionsWorker
from vocode.streaming.agent.base_agent import (
    AgentResponse,
    AgentResponseFillerAudio,
    AgentResponseMessage,
    AgentResponseStop,
    BaseAgent,
    TranscriptionAgentInput,
)
from vocode.streaming.constants import (
    ALLOWED_IDLE_TIME,
    CHECK_HUMAN_PRESENT_MESSAGE_CHOICES,
    TEXT_TO_SPEECH_CHUNK_SIZE_SECONDS,
)
from vocode.streaming.models.actions import EndOfTurn
from vocode.streaming.models.agent import FillerAudioConfig
from vocode.streaming.models.events import Sender
from vocode.streaming.models.message import (
    BaseMessage,
    BotBackchannel,
    LLMToken,
    SilenceMessage,
)
from vocode.streaming.models.telephony import (
    IvrConfig,
    IvrDagConfig,
    IvrHoldNode,
    IvrLinkType,
    IvrMessageNode,
    IvrPlayNode,
)
from vocode.streaming.models.transcriber import TranscriberConfig, Transcription
from vocode.streaming.models.transcript import (
    Message,
    Transcript,
    TranscriptCompleteEvent,
)
from vocode.streaming.output_device.audio_chunk import AudioChunk, ChunkState
from vocode.streaming.synthesizer.base_synthesizer import (
    BaseSynthesizer,
    FillerAudio,
    SynthesisResult,
)
from vocode.streaming.synthesizer.input_streaming_synthesizer import (
    InputStreamingSynthesizer,
)
from vocode.streaming.telephony.constants import (
    DEFAULT_HOLD_DURATION,
    DEFAULT_HOLD_MESSAGE_DELAY,
)
from vocode.streaming.transcriber.base_transcriber import BaseTranscriber
from vocode.streaming.transcriber.deepgram_transcriber import DeepgramTranscriber
from vocode.streaming.utils import (
    create_conversation_id,
    enumerate_async_iter,
    get_chunk_size_per_second,
)
from vocode.streaming.utils.audio_pipeline import AudioPipeline, OutputDeviceType
from vocode.streaming.utils.create_task import asyncio_create_task
from vocode.streaming.utils.events_manager import EventsManager
from vocode.streaming.utils.speed_manager import SpeedManager
from vocode.streaming.utils.state_manager import ConversationStateManager
from vocode.streaming.utils.worker import (
    AbstractWorker,
    AsyncQueueWorker,
    InterruptibleAgentResponseEvent,
    InterruptibleEvent,
    InterruptibleEventFactory,
    InterruptibleWorker,
)
from vocode.utils.sentry_utils import (
    CustomSentrySpans,
    complete_span_by_op,
    sentry_create_span,
    synthesizer_base_name_if_should_report_to_sentry,
)

BACKCHANNEL_PATTERNS = [
    r"m+-?hm+",
    r"m+",
    r"oh+",
    r"ah+",
    r"um+",
    r"uh+",
    "yes",
    "sure",
    "quite",
    "right",
    "really",
    "good heavens",
    "i see",
    "of course",
    "oh dear",
    "oh god",
    "thats nice",
    "thats not bad",
    "thats right",
    r"yeah+",
    "makes sense",
]
LOW_INTERRUPT_SENSITIVITY_BACKCHANNEL_UTTERANCE_LENGTH_THRESHOLD = 3
LOWEST_INTERRUPT_SENSITIVITY_BACKCHANNEL_UTTERANCE_LENGTH_THRESHOLD = 50

AUDIO_FILES_PATH = os.path.join(os.path.dirname(__file__), "audio_files")
AUDIO_FRAME_RATE = 8000
AUDIO_SAMPLE_WIDTH = 1
AUDIO_CHANNELS = 1


class StreamingConversation(AudioPipeline[OutputDeviceType]):
    class QueueingInterruptibleEventFactory(InterruptibleEventFactory):
        def __init__(self, conversation: "StreamingConversation"):
            self.conversation = conversation

        def create_interruptible_event(
            self,
            payload: Any,
            is_interruptible: bool = True,
        ) -> InterruptibleEvent[Any]:
            interruptible_event: InterruptibleEvent = super().create_interruptible_event(
                payload,
                is_interruptible,
            )
            self.conversation.interruptible_events.put_nowait(interruptible_event)
            return interruptible_event

        def create_interruptible_agent_response_event(
            self,
            payload: Any,
            is_interruptible: bool = True,
            agent_response_tracker: Optional[asyncio.Event] = None,
        ) -> InterruptibleAgentResponseEvent:
            interruptible_event = super().create_interruptible_agent_response_event(
                payload,
                is_interruptible=is_interruptible,
                agent_response_tracker=agent_response_tracker,
            )
            self.conversation.interruptible_events.put_nowait(interruptible_event)
            return interruptible_event

    class TranscriptionsWorker(AsyncQueueWorker[Transcription]):
        """Processes all transcriptions: sends an interrupt if needed
        and sends final transcriptions to the output queue"""

        consumer: AbstractWorker[InterruptibleEvent[Transcription]]

        def __init__(
            self,
            conversation: "StreamingConversation",
            interruptible_event_factory: InterruptibleEventFactory,
        ):
            super().__init__()
            self.conversation = conversation
            self.interruptible_event_factory = interruptible_event_factory
            self.in_interrupt_endpointing_config = False
            self.deepgram_transcriber: Optional[DeepgramTranscriber] = None
            if isinstance(self.conversation.transcriber, DeepgramTranscriber):
                self.deepgram_transcriber = self.conversation.transcriber
            self.has_associated_ignored_utterance: bool = False
            self.has_associated_unignored_utterance: bool = False
            self.human_backchannels_buffer: List[Transcription] = []
            self.ignore_next_message: bool = False
            self.simulate_interrupt = (
                self.conversation.transcriber.transcriber_config.endpointing_config.simulate_interrupt
            )
            self.last_transcription: Optional[str] = None
            self.last_transcription_time: Optional[float] = None

        def should_ignore_utterance(self, transcription: Transcription):
            if self.has_associated_unignored_utterance and not self.simulate_interrupt:
                return False
            bot_still_speaking = self.is_bot_still_speaking()
            if self.has_associated_ignored_utterance or bot_still_speaking:
                logger.info(
                    f"Associated ignored utterance: {self.has_associated_ignored_utterance}. Bot still speaking: {bot_still_speaking}"
                )
                return self.is_transcription_backchannel(transcription)
            return False

        def is_transcription_backchannel(self, transcription: Transcription):
            num_words = len(transcription.message.strip().split())
            if (
                self.conversation.agent.get_agent_config().interrupt_sensitivity == "high"
                and num_words >= 1
            ):
                logger.info(f"High interrupt sensitivity; {num_words} word(s) not a backchannel")
                return False

            threshold = LOW_INTERRUPT_SENSITIVITY_BACKCHANNEL_UTTERANCE_LENGTH_THRESHOLD
            if self.simulate_interrupt:
                # When simulating interruptions, we need a high threshold so we don't get
                # interrupted when we're just delivering the interruption.
                threshold = LOWEST_INTERRUPT_SENSITIVITY_BACKCHANNEL_UTTERANCE_LENGTH_THRESHOLD

            if num_words <= threshold:
                return True
            cleaned = re.sub("[^\w\s]", "", transcription.message).strip().lower()
            return any(re.fullmatch(regex, cleaned) for regex in BACKCHANNEL_PATTERNS)

        def _most_recent_transcript_messages(self) -> Iterator[Message]:
            return (
                event_log
                for event_log in reversed(self.conversation.transcript.event_logs)
                if isinstance(event_log, Message)
            )

        def get_maybe_last_transcript_event_log(self) -> Optional[Message]:
            return next(self._most_recent_transcript_messages(), None)

        def is_bot_in_medias_res(self):
            last_message = self.get_maybe_last_transcript_event_log()
            return (
                last_message is not None
                and not last_message.is_backchannel
                and last_message.sender == Sender.BOT
                and not last_message.is_final
                and last_message.text.strip() != ""
            )

        def is_bot_still_speaking(self):  # in_medias_res OR bot has more utterances
            transcript_messages_iter = self._most_recent_transcript_messages()
            last_message, second_to_last_message = next(transcript_messages_iter, None), next(
                transcript_messages_iter, None
            )

            is_first_bot_message = (
                second_to_last_message is None or second_to_last_message.sender == Sender.HUMAN
            )

            return (
                last_message is not None
                and not last_message.is_backchannel
                and last_message.sender == Sender.BOT
                and (not last_message.is_final or not last_message.is_end_of_turn)
                and not (is_first_bot_message and last_message.text.strip() == "")
            )

        async def process(self, transcription: Transcription):
            # Deduplicate transcriptions within a small time window
            current_time = time.time()
            if (
                self.last_transcription == transcription.message
                and self.last_transcription_time
                and current_time - self.last_transcription_time < 0.5
            ):
                logger.debug(f"Ignoring duplicate transcription: {transcription.message}")
                return

            self.last_transcription = transcription.message
            self.last_transcription_time = current_time

            self.conversation.mark_last_action_timestamp()
            if transcription.message.strip() == "":
                logger.info("Ignoring empty transcription")
                return
            # ignore utterances during the initial message but still add them to the transcript
            initial_message_ongoing = not self.conversation.initial_message_tracker.is_set()
            if initial_message_ongoing or self.should_ignore_utterance(transcription):
                logger.info(
                    f"Ignoring utterance: {transcription.message}. IMO: {initial_message_ongoing}"
                )
                self.has_associated_ignored_utterance = (
                    not transcription.is_final  # if it's final, we're done with this backchannel
                )
                if transcription.is_final:
                    # for all ignored backchannels, store them to be added to the transcript later
                    self.human_backchannels_buffer.append(transcription)
                return
            if self.ignore_next_message and transcription.is_final:
                # TODO: delete this once transcription reset is implemented for processing conference voicemail
                # Push human message to transcript but do not respond
                self.has_associated_ignored_utterance = False
                agent_response_tracker = None
                self.ignore_next_message = False
                return
            if transcription.is_final:
                if (
                    self.deepgram_transcriber is not None
                    and self.deepgram_transcriber.is_first_transcription
                ):
                    logger.debug(
                        "Switching to non-first transcription endpointing config if configured"
                    )
                    self.deepgram_transcriber.is_first_transcription = False
                logger.debug(
                    "Got final transcription: {}, confidence: {}, wpm: {}".format(
                        transcription.message,
                        transcription.confidence,
                        transcription.wpm(),
                    )
                )
            bot_was_in_medias_res = self.is_bot_in_medias_res() or self.is_bot_still_speaking()
            if self.conversation.is_human_speaking:
                logger.debug("Human started speaking")
                if bot_was_in_medias_res:
                    self.conversation.current_transcription_is_interrupt = (
                        await self.conversation.broadcast_interrupt()
                    )
                    self.has_associated_unignored_utterance = not transcription.is_final
                    if self.conversation.current_transcription_is_interrupt:
                        logger.debug("sent interrupt")
                else:
                    self.conversation.current_transcription_is_interrupt = False

            transcription.is_interrupt = self.conversation.current_transcription_is_interrupt
            self.conversation.is_human_speaking = not transcription.is_final
            if transcription.is_final:
                self.has_associated_ignored_utterance = False
                self.has_associated_unignored_utterance = False
                agent_response_tracker = None

                # clear out backchannels and add to the transcript
                for human_backchannel in self.human_backchannels_buffer:
                    self.conversation.transcript.add_human_message(
                        text=human_backchannel.message,
                        conversation_id=self.conversation.id,
                        is_backchannel=True,
                    )
                self.human_backchannels_buffer = []

                if transcription.is_interrupt:
                    transcription.bot_was_in_medias_res = bot_was_in_medias_res
                    logger.debug(
                        f"Bot is {'not ' if not transcription.bot_was_in_medias_res else ''}in medias res"
                    )

                self.conversation.speed_manager.update(transcription)

                self.conversation.warmup_synthesizer()

                # we use getattr here to avoid the dependency cycle between PhoneConversation and StreamingConversation
                event = self.interruptible_event_factory.create_interruptible_event(
                    TranscriptionAgentInput(
                        transcription=transcription,
                        conversation_id=self.conversation.id,
                        vonage_uuid=getattr(self.conversation, "vonage_uuid", None),
                        twilio_sid=getattr(self.conversation, "twilio_sid", None),
                        agent_response_tracker=agent_response_tracker,
                    ),
                )
                self.consumer.consume_nonblocking(event)

    class FillerAudioWorker(InterruptibleWorker[InterruptibleAgentResponseEvent[FillerAudio]]):
        """
        - Waits for a configured number of seconds and then sends filler audio to the output
        - Exposes wait_for_filler_audio_to_finish() which the AgentResponsesWorker waits on before
          sending responses to the output queue
        """

        def __init__(
            self,
            conversation: "StreamingConversation",
        ):
            super().__init__()
            self.conversation = conversation
            self.current_filler_seconds_per_chunk: Optional[int] = None
            self.filler_audio_started_event: Optional[threading.Event] = None

        async def wait_for_filler_audio_to_finish(self):
            if self.filler_audio_started_event is None or not self.filler_audio_started_event.set():
                logger.debug(
                    "Not waiting for filler audio to finish since we didn't send any chunks",
                )
                return
            if self.interruptible_event and isinstance(
                self.interruptible_event,
                InterruptibleAgentResponseEvent,
            ):
                await self.interruptible_event.agent_response_tracker.wait()

        def interrupt_current_filler_audio(self):
            return self.interruptible_event and self.interruptible_event.interrupt()

        async def process(self, item: InterruptibleAgentResponseEvent[FillerAudio]):
            try:
                filler_audio = item.payload
                assert self.conversation.filler_audio_config is not None
                filler_synthesis_result = filler_audio.create_synthesis_result()
                self.current_filler_seconds_per_chunk = filler_audio.seconds_per_chunk
                silence_threshold = self.conversation.filler_audio_config.silence_threshold_seconds
                await asyncio.sleep(silence_threshold)
                logger.debug("Sending filler audio to output")
                self.filler_audio_started_event = threading.Event()
                await self.conversation.send_speech_to_output(
                    filler_audio.message.text,
                    filler_synthesis_result,
                    item.interruption_event,
                    filler_audio.seconds_per_chunk,
                    started_event=self.filler_audio_started_event,
                )
                item.agent_response_tracker.set()
            except asyncio.CancelledError:
                pass

    class AgentResponsesWorker(InterruptibleWorker[InterruptibleAgentResponseEvent[AgentResponse]]):
        """Runs Synthesizer.create_speech and sends the SynthesisResult to the output queue"""

        consumer: AbstractWorker[
            InterruptibleAgentResponseEvent[
                Tuple[Union[BaseMessage, EndOfTurn], Optional[SynthesisResult]]
            ]
        ]

        def __init__(
            self,
            conversation: "StreamingConversation",
            interruptible_event_factory: InterruptibleEventFactory,
        ):
            super().__init__()
            self.conversation = conversation
            self.interruptible_event_factory = interruptible_event_factory
            self.chunk_size = 960  # self.conversation._get_synthesizer_chunk_size()
            self.last_agent_response_tracker: Optional[asyncio.Event] = None
            self.is_first_text_chunk = True

        def send_filler_audio(self, agent_response_tracker: Optional[asyncio.Event]):
            assert self.conversation.filler_audio_worker is not None
            logger.debug("Sending filler audio")
            if self.conversation.synthesizer.filler_audios:
                filler_audio = random.choice(self.conversation.synthesizer.filler_audios)
                logger.debug(f"Chose {filler_audio.message.text}")
                event = self.interruptible_event_factory.create_interruptible_agent_response_event(
                    filler_audio,
                    is_interruptible=filler_audio.is_interruptible,
                    agent_response_tracker=agent_response_tracker,
                )
                logger.debug(f"Consuming filler audio event: {event}")
                self.conversation.filler_audio_worker.consume_nonblocking(event)
            else:
                logger.debug("No filler audio available for synthesizer")

        async def process(self, item: InterruptibleAgentResponseEvent[AgentResponse]):
            if not self.conversation.synthesis_enabled:
                logger.debug("Synthesis disabled, not synthesizing speech")
                return
            try:
                logger.debug(f"Consuming agent response event: {item.payload}")
                agent_response = item.payload
                if isinstance(agent_response, AgentResponseFillerAudio):
                    self.send_filler_audio(item.agent_response_tracker)
                    return
                if isinstance(agent_response, AgentResponseStop):
                    logger.debug("Agent requested to stop")
                    if self.last_agent_response_tracker is not None:
                        await self.last_agent_response_tracker.wait()
                    item.agent_response_tracker.set()
                    self.conversation.mark_terminated(bot_disconnect=True)
                    return

                agent_response_message = typing.cast(AgentResponseMessage, agent_response)

                if self.conversation.filler_audio_worker is not None:
                    if self.conversation.filler_audio_worker.interrupt_current_filler_audio():
                        await self.conversation.filler_audio_worker.wait_for_filler_audio_to_finish()

                if isinstance(agent_response_message.message, EndOfTurn):
                    logger.debug(
                        "Sending end of turn for message: {}".format(agent_response_message)
                    )

                    self.consumer.consume_nonblocking(
                        self.interruptible_event_factory.create_interruptible_agent_response_event(
                            (agent_response_message.message, None),
                            is_interruptible=item.is_interruptible,
                            agent_response_tracker=item.agent_response_tracker,
                        ),
                    )
                    if isinstance(self.conversation.synthesizer, InputStreamingSynthesizer):
                        await self.conversation.synthesizer.handle_end_of_turn()
                    self.is_first_text_chunk = True
                    return

                synthesizer_base_name: Optional[str] = (
                    synthesizer_base_name_if_should_report_to_sentry(self.conversation.synthesizer)
                )
                create_speech_span: Optional[Span] = None
                ttft_span: Optional[Span] = None
                synthesis_span: Optional[Span] = None
                if synthesizer_base_name and agent_response_message.is_first:
                    complete_span_by_op(CustomSentrySpans.LANGUAGE_MODEL_TIME_TO_FIRST_TOKEN)

                    sentry_create_span(
                        sentry_callable=sentry_sdk.start_span,
                        op=CustomSentrySpans.SYNTHESIS_TIME_TO_FIRST_TOKEN,
                    )

                    synthesis_span = sentry_create_span(
                        sentry_callable=sentry_sdk.start_span,
                        op=f"{synthesizer_base_name}{CustomSentrySpans.SYNTHESIZER_SYNTHESIS_TOTAL}",
                    )
                    if synthesis_span:
                        ttft_span = sentry_create_span(
                            sentry_callable=synthesis_span.start_child,
                            op=f"{synthesizer_base_name}{CustomSentrySpans.SYNTHESIZER_TIME_TO_FIRST_TOKEN}",
                        )
                    if ttft_span:
                        create_speech_span = sentry_create_span(
                            sentry_callable=ttft_span.start_child,
                            op=f"{synthesizer_base_name}{CustomSentrySpans.SYNTHESIZER_CREATE_SPEECH}",
                        )
                maybe_synthesis_result: Optional[SynthesisResult] = None
                if isinstance(
                    self.conversation.synthesizer,
                    InputStreamingSynthesizer,
                ) and isinstance(agent_response_message.message, LLMToken):
                    logger.debug("Sending chunk to synthesizer")
                    await self.conversation.synthesizer.send_token_to_synthesizer(
                        message=agent_response_message.message,
                        chunk_size=self.chunk_size,
                    )
                else:
                    logger.debug("Synthesizing speech for message")
                    maybe_synthesis_result = await self.conversation.synthesizer.create_speech(
                        agent_response_message.message,
                        self.chunk_size,
                        is_first_text_chunk=self.is_first_text_chunk,
                        is_sole_text_chunk=agent_response_message.is_sole_text_chunk,
                    )
                if create_speech_span:
                    create_speech_span.finish()
                # For input streaming synthesizers, subsequent chunks are contained in the same SynthesisResult
                if isinstance(self.conversation.synthesizer, InputStreamingSynthesizer):
                    if not self.is_first_text_chunk:
                        maybe_synthesis_result = None
                    elif isinstance(agent_response_message.message, LLMToken):
                        maybe_synthesis_result = (
                            self.conversation.synthesizer.get_current_utterance_synthesis_result()
                        )
                if maybe_synthesis_result is not None:
                    synthesis_result = maybe_synthesis_result
                    synthesis_result.is_first = agent_response_message.is_first
                    if not synthesis_result.cached and synthesis_span:
                        synthesis_result.synthesis_total_span = synthesis_span
                        synthesis_result.ttft_span = ttft_span
                    self.consumer.consume_nonblocking(
                        self.interruptible_event_factory.create_interruptible_agent_response_event(
                            (agent_response_message.message, synthesis_result),
                            is_interruptible=item.is_interruptible,
                            agent_response_tracker=item.agent_response_tracker,
                        ),
                    )
                self.last_agent_response_tracker = item.agent_response_tracker
                if not isinstance(agent_response_message.message, SilenceMessage):
                    self.is_first_text_chunk = False
            except asyncio.CancelledError:
                pass

    class SynthesisResultsWorker(
        AsyncQueueWorker[Tuple[Union[BaseMessage, EndOfTurn], Optional[SynthesisResult]]]
    ):
        """Plays SynthesisResults from the output queue on the output device"""

        def __init__(
            self,
            conversation: "StreamingConversation",
        ):
            super().__init__()
            self.conversation = conversation
            self.last_transcript_message: Optional[Message] = None

        async def process(
            self,
            item: InterruptibleAgentResponseEvent[
                Tuple[Union[BaseMessage, EndOfTurn], Optional[SynthesisResult]]
            ],
        ):
            try:
                message, synthesis_result = item.payload

                # Now handle the message type
                if isinstance(message, EndOfTurn):
                    if self.last_transcript_message is not None:
                        self.last_transcript_message.is_end_of_turn = True
                    item.agent_response_tracker.set()
                    return
                # create an empty transcript message and attach it to the transcript
                transcript_message = Message(
                    text="",
                    sender=Sender.BOT,
                    is_backchannel=isinstance(message, BotBackchannel),
                )
                if not isinstance(message, SilenceMessage):
                    self.conversation.transcript.add_message(
                        message=transcript_message,
                        conversation_id=self.conversation.id,
                        publish_to_events_manager=False,
                    )
                if isinstance(message, SilenceMessage):
                    logger.debug(f"Sending {message.trailing_silence_seconds} seconds of silence")
                elif isinstance(message, BotBackchannel):
                    logger.debug(f"Sending backchannel: {message}")
                message_sent, cut_off = await self.conversation.send_speech_to_output(
                    message.text,
                    synthesis_result,
                    item.interruption_event,
                    TEXT_TO_SPEECH_CHUNK_SIZE_SECONDS,
                    transcript_message=transcript_message,
                )
                # publish the transcript message now that it includes what was said during send_speech_to_output
                self.conversation.transcript.maybe_publish_transcript_event_from_message(
                    message=transcript_message,
                    conversation_id=self.conversation.id,
                )
                item.agent_response_tracker.set()
                logger.debug("Message sent: {}".format(message_sent))
                if cut_off:
                    self.conversation.agent.update_last_bot_message_on_cut_off(message_sent)
                self.last_transcript_message = transcript_message

            except asyncio.CancelledError:
                pass

    class IvrWorker(InterruptibleWorker[InterruptibleEvent[Transcription]]):
        def __init__(
            self,
            conversation: "StreamingConversation",
            dag: IvrDagConfig,
        ):
            super().__init__()
            self.conversation = conversation
            self.dag = dag
            self._current_node_id: Optional[str] = None
            self._run_task: Optional[asyncio.Task] = None
            self._listen_queue: Optional[asyncio.Queue[str]] = None
            self._listen_queue_lock = asyncio.Lock()
            self._dtmf_queue: Optional[asyncio.Queue[str]] = None
            self._dtmf_queue_lock = asyncio.Lock()
            self._finished_event = asyncio.Event()
            self.fuzz_threshold = dag.fuzz_threshold or 80

        def start(self):
            super().start()
            self._current_node_id = self.dag.start
            self._run_task = asyncio_create_task(self._run())

        async def terminate(self) -> bool:
            await super().terminate()
            if self._run_task:
                return await self._run_task.cancel()
            return False

        async def _get_listen_queue(self) -> Optional[asyncio.Queue[str]]:
            async with self._listen_queue_lock:
                if self._listen_queue is None:
                    return None
                return self._listen_queue

        async def _set_listen_queue(self, listen_queue: asyncio.Queue[str]):
            async with self._listen_queue_lock:
                self._listen_queue = listen_queue

        async def _get_dtmf_queue(self) -> Optional[asyncio.Queue[str]]:
            async with self._dtmf_queue_lock:
                return self._dtmf_queue

        async def _set_dtmf_queue(self, dtmf_queue: asyncio.Queue[str]):
            async with self._dtmf_queue_lock:
                self._dtmf_queue = dtmf_queue

        async def receive_dtmf(self, digit: str):
            dtmf_queue = await self._get_dtmf_queue()
            if dtmf_queue:
                dtmf_queue.put_nowait(digit)

        async def _send_single_message(self, message: str):
            message_tracker = asyncio.Event()
            await self.conversation.send_single_message(
                message=BaseMessage(text=message),
                message_tracker=message_tracker,
            )
            await message_tracker.wait()

        async def _loop_message(self, message: str):
            try:
                while True:
                    await self._send_single_message(message)
                    await asyncio.sleep(10)
            except asyncio.CancelledError:
                logger.debug("IvrWorker _loop_message done")
                return

        async def _run(self):
            while True:
                try:
                    current_node = self.dag.nodes[self._current_node_id]
                    logger.debug(
                        f"IvrWorker current node: {current_node}, type: {type(current_node)}"
                    )

                    if current_node.wait_delay:
                        await asyncio.sleep(current_node.wait_delay)

                    if current_node.is_final:
                        logger.debug("IvrWorker is finished")
                        break

                    if isinstance(current_node, IvrPlayNode):
                        await self.conversation.play_audio(current_node.sound)
                        if current_node.links:
                            if len(current_node.links) > 1:
                                logger.error(
                                    "IVR DAG has multiple links for play node, using the first one"
                                )
                            self._current_node_id = current_node.links[0].next
                        if current_node.delay:
                            await asyncio.sleep(current_node.delay)

                    elif isinstance(current_node, IvrMessageNode):
                        loop_task = asyncio_create_task(self._loop_message(current_node.message))

                        available_commands = [link.message for link in current_node.links]
                        if current_node.link_type == IvrLinkType.COMMAND:
                            command = await self.wait_for_command(available_commands)
                        elif current_node.link_type == IvrLinkType.DTMF:
                            command = await self.wait_for_dtmf(available_commands)
                        else:
                            raise Exception(
                                f"IvrWorker unknown link type: {current_node.link_type}"
                            )

                        loop_task.cancel()
                        await loop_task

                        next_node = [
                            link.next for link in current_node.links if link.message == command
                        ]
                        if not next_node:
                            raise Exception(f"IvrWorker no next node found for command: {command}")
                        self._current_node_id = next_node[0]

                    elif isinstance(current_node, IvrHoldNode):
                        start_time = time.time()

                        def is_done():
                            return time.time() - start_time >= current_node.duration

                        while not is_done():
                            for msg in current_node.messages:
                                message_tracker = asyncio.Event()
                                await self.conversation.send_single_message(
                                    message=BaseMessage(text=msg),
                                    message_tracker=message_tracker,
                                )
                                await message_tracker.wait()
                                await asyncio.sleep(current_node.delay)
                                if is_done():
                                    break
                        if current_node.links:
                            if len(current_node.links) > 1:
                                logger.error(
                                    "IVR DAG has multiple links at the end of hold node, using the first one"
                                )
                            self._current_node_id = current_node.links[0].next
                        else:
                            raise Exception("IVR DAG has no links at the end of hold node")
                    else:
                        raise Exception(f"IVR DAG node {current_node} not supported")

                except asyncio.CancelledError:
                    return

            self._finished_event.set()

        async def wait_for_finished(self):
            await self._finished_event.wait()

        async def wait_for_command(self, available_commands: List[str]) -> str:
            listen_queue = asyncio.Queue()
            await self._set_listen_queue(listen_queue)

            while True:
                logger.debug("IvrWorker waiting for commands: {}".format(available_commands))
                received_message = await self._listen_queue.get()
                for command in available_commands:
                    match_score = fuzz.partial_ratio(command.lower(), received_message.lower())
                    if match_score >= self.fuzz_threshold:
                        logger.debug(
                            f"IVR received MATCHING command: {command} with score {match_score}"
                        )
                        await self._set_listen_queue(None)
                        return command
                else:
                    logger.debug(
                        f"IVR received command: {received_message}, but {available_commands} were expected"
                    )

        async def wait_for_dtmf(self, available_commands: List[str]) -> str:
            dtmf_queue = asyncio.Queue()
            await self._set_dtmf_queue(dtmf_queue)

            while True:
                digit = await self._dtmf_queue.get()
                for command in available_commands:
                    if command.lower() == digit.lower():
                        logger.debug(f"IVR received DTMF command: {command}")
                        await self._set_dtmf_queue(None)
                        return command

        async def process(self, item: InterruptibleEvent[TranscriptionAgentInput]):
            transcription = item.payload.transcription
            if not transcription.is_final:
                logger.debug(f"IVR received non-final transcription: {transcription.message}")
                return
            logger.debug(f"IVR received transcription: {transcription.message}")
            listen_queue = await self._get_listen_queue()
            if not listen_queue:
                logger.debug(f"IVR not listening, skipping {transcription.message}")
                return

            listen_queue.put_nowait(transcription.message)

    def __init__(
        self,
        output_device: OutputDeviceType,
        transcriber: BaseTranscriber[TranscriberConfig],
        agent: BaseAgent,
        synthesizer: BaseSynthesizer,
        speed_coefficient: float = 1.0,
        conversation_id: Optional[str] = None,
        events_manager: Optional[EventsManager] = None,
        ivr_config: Optional[IvrConfig] = None,
        ivr_dag: Optional[IvrDagConfig] = None,
    ):
        self.id = conversation_id or create_conversation_id()
        ctx_conversation_id.set(self.id)

        self.output_device = output_device
        self.transcriber = transcriber
        self.agent = agent
        self.synthesizer = synthesizer
        self.synthesis_enabled = True
        self.ivr_config = ivr_config

        self.interruptible_events: queue.Queue[InterruptibleEvent] = queue.Queue()
        self.interruptible_event_factory = self.QueueingInterruptibleEventFactory(conversation=self)
        self.synthesis_results_queue: asyncio.Queue[
            InterruptibleAgentResponseEvent[
                Tuple[Union[BaseMessage, EndOfTurn], Optional[SynthesisResult]]
            ]
        ] = asyncio.Queue()
        self.state_manager = self.create_state_manager()

        # Transcriptions Worker
        self.transcriptions_worker = self.TranscriptionsWorker(
            conversation=self,
            interruptible_event_factory=self.interruptible_event_factory,
        )
        self.transcriber.consumer = self.transcriptions_worker

        # Agent
        self.transcriptions_worker.consumer = self.agent
        self.agent.set_interruptible_event_factory(self.interruptible_event_factory)
        self.agent.attach_conversation_state_manager(self.state_manager)

        # Agent Responses Worker
        self.agent_responses_worker = self.AgentResponsesWorker(
            conversation=self,
            interruptible_event_factory=self.interruptible_event_factory,
        )
        self.agent.agent_responses_consumer = self.agent_responses_worker

        # Actions Worker
        self.actions_worker = None
        if self.agent.get_agent_config().actions:
            self.actions_worker = ActionsWorker(
                action_factory=self.agent.action_factory,
                interruptible_event_factory=self.interruptible_event_factory,
            )
            self.actions_worker.attach_conversation_state_manager(self.state_manager)
            self.actions_worker.consumer = self.agent
            self.agent.actions_consumer = self.actions_worker

        # Synthesis Results Worker
        self.synthesis_results_worker = self.SynthesisResultsWorker(conversation=self)
        self.agent_responses_worker.consumer = self.synthesis_results_worker

        # Filler Audio Worker
        self.filler_audio_worker = None
        self.filler_audio_config: Optional[FillerAudioConfig] = None
        if self.agent.get_agent_config().send_filler_audio:
            self.filler_audio_worker = self.FillerAudioWorker(conversation=self)

        self.speed_coefficient = speed_coefficient
        self.speed_manager = SpeedManager(
            speed_coefficient=self.speed_coefficient,
        )
        self.transcriber.attach_speed_manager(self.speed_manager)
        self.agent.attach_speed_manager(self.speed_manager)

        self.should_run_events_manager = False
        self.events_manager = events_manager
        if not self.events_manager:
            self.should_run_events_manager = True
            self.events_manager = EventsManager()

        self.events_task: Optional[asyncio.Task] = None
        self.transcript = Transcript()
        self.transcript.attach_events_manager(self.events_manager)

        self.is_human_speaking = False
        self.is_terminated = asyncio.Event()
        self.mark_last_action_timestamp()

        self.check_for_idle_task: Optional[asyncio.Task] = None
        self.check_for_idle_paused = False

        self.current_transcription_is_interrupt: bool = False

        self.initial_message_tracker = asyncio.Event()

        if ivr_dag:
            self.ivr_worker = self.IvrWorker(conversation=self, dag=ivr_dag)
            self.transcriptions_worker.consumer = self.ivr_worker
        else:
            self.ivr_worker = None

        # tracing
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None

        self.idle_time_threshold = (
            self.agent.get_agent_config().allowed_idle_time_seconds or ALLOWED_IDLE_TIME
        )

        self.interrupt_lock = asyncio.Lock()

    def create_state_manager(self) -> ConversationStateManager:
        return ConversationStateManager(conversation=self)

    async def start(self, mark_ready: Optional[Callable[[], Awaitable[None]]] = None):
        self._audio_segments = self._load_audio_segments()
        self.transcriber.start()
        self.transcriber.streaming_conversation = self
        self.transcriptions_worker.start()
        self.agent_responses_worker.start()
        self.synthesis_results_worker.start()
        self.synthesizer.streaming_conversation = self
        self.output_device.start()
        if self.filler_audio_worker is not None:
            self.filler_audio_worker.start()
        if self.actions_worker is not None:
            self.actions_worker.start()
        is_ready = await self.transcriber.ready()
        if not is_ready:
            raise Exception("Transcriber startup failed")
        if self.agent.get_agent_config().send_filler_audio:
            if not isinstance(self.agent.get_agent_config().send_filler_audio, FillerAudioConfig):
                self.filler_audio_config = FillerAudioConfig()
            else:
                self.filler_audio_config = typing.cast(
                    FillerAudioConfig,
                    self.agent.get_agent_config().send_filler_audio,
                )
            await self.synthesizer.set_filler_audios(self.filler_audio_config)

        self.agent.start()
        initial_message = self.agent.get_agent_config().initial_message

        if self.ivr_worker:
            self.ivr_worker.start()
            asyncio_create_task(self._ivr_handoff(initial_message))
            self.initial_message_tracker.set()
        elif self.ivr_config:
            asyncio_create_task(self.handle_ivr())
        elif initial_message:
            asyncio_create_task(
                self.send_initial_message(initial_message, self.initial_message_tracker),
            )
        else:
            self.initial_message_tracker.set()
        self.agent.attach_transcript(self.transcript)
        if mark_ready:
            await mark_ready()
        self.is_terminated.clear()
        self.check_for_idle_task = asyncio_create_task(
            self.check_for_idle(),
        )
        if self.should_run_events_manager and len(self.events_manager.subscriptions) > 0:
            self.events_task = asyncio_create_task(
                self.events_manager.start(),
            )
        elif not self.should_run_events_manager:
            logger.debug("events_manager task is managed by the caller")
        else:
            logger.debug("events_manager.subscriptions is empty")

    async def _ivr_handoff(self, initial_message: Optional[BaseMessage] = None):
        logger.debug("Waiting for IVR handoff")
        await self.ivr_worker.wait_for_finished()

        logger.debug("IVR handoff complete, restarting transcriptions worker")
        await self.transcriptions_worker.terminate()

        if initial_message:
            initial_message_tracker = asyncio.Event()
            await self.send_initial_message(initial_message, initial_message_tracker)

        self.transcriptions_worker.consumer = self.agent
        self.transcriptions_worker.start()

    def set_check_for_idle_paused(self, paused: bool):
        logger.debug(f"Setting idle check paused to {paused}")
        if not paused:
            self.mark_last_action_timestamp()
        self.check_for_idle_paused = paused

    async def send_initial_message(
        self,
        initial_message: BaseMessage,
        initial_message_tracker: asyncio.Event,
    ):
        # TODO: configure if initial message is interruptible
        delay = self.agent.get_agent_config().initial_message_delay
        if delay > 0:
            logger.info(f"Waiting {delay} seconds before initial message")
            await asyncio.sleep(delay)
        await self.send_single_message(
            message=initial_message,
            message_tracker=initial_message_tracker,
        )
        await initial_message_tracker.wait()

    async def handle_ivr(self):
        self.set_check_for_idle_paused(True)
        ivr_message_tracker = asyncio.Event()
        if self.ivr_config.ivr_message:
            await self.send_single_message(
                message=BaseMessage(text=self.ivr_config.ivr_message),
                message_tracker=ivr_message_tracker,
            )
            await ivr_message_tracker.wait()

        if self.ivr_config.ivr_handoff_delay:
            await asyncio.sleep(self.ivr_config.ivr_handoff_delay)

        if self.ivr_config.hold_message:
            hold_start_time = time.time()
            hold_message_delay = self.ivr_config.hold_message_delay or DEFAULT_HOLD_MESSAGE_DELAY
            hold_duration = self.ivr_config.hold_duration or DEFAULT_HOLD_DURATION

            while time.time() - hold_start_time < hold_duration:
                hold_message_tracker = asyncio.Event()
                await self.send_single_message(
                    message=BaseMessage(text=self.ivr_config.hold_message),
                    message_tracker=hold_message_tracker,
                )
                await hold_message_tracker.wait()

                remaining_time = self.ivr_config.hold_duration - (time.time() - hold_start_time)
                if remaining_time > 0:
                    await asyncio.sleep(min(remaining_time, hold_message_delay))

        self.set_check_for_idle_paused(False)
        self.initial_message_tracker.set()

    async def receive_dtmf(self, digit: str):
        logger.debug(f"Received DTMF digit: {digit}")
        if self.ivr_worker:
            await self.ivr_worker.receive_dtmf(digit)

    async def action_on_idle(self):
        logger.debug("Conversation idle for too long, terminating")
        self.mark_terminated(bot_disconnect=True)
        return

    async def check_for_idle(self):
        """Asks if human is still on the line if no activity is detected."""
        await self.initial_message_tracker.wait()
        logger.debug(
            "Starting idle check, last_action_timestamp={}, idle_threshold={}",
            self.last_action_timestamp,
            self.idle_time_threshold,
        )

        check_human_present_count = 0
        check_human_present_threshold = self.agent.get_agent_config().num_check_human_present_times

        while self.is_active():
            current_time = time.time()
            time_since_last_action = current_time - self.last_action_timestamp

            if not self.check_for_idle_paused and time_since_last_action > self.idle_time_threshold:
                logger.debug(f"Time since last action: {time_since_last_action:.2f}s")
                if check_human_present_count >= check_human_present_threshold:
                    await self.action_on_idle()
                await self.send_single_message(
                    message=BaseMessage(text=random.choice(CHECK_HUMAN_PRESENT_MESSAGE_CHOICES)),
                )
                check_human_present_count += 1

            await asyncio.sleep(ALLOWED_IDLE_TIME)

    def _load_audio_segments(self) -> Dict[str, bytes]:
        audio_segments = {}
        for sound in ["beep", "ring"]:
            try:
                audio_segment = AudioSegment.from_file(
                    os.path.join(AUDIO_FILES_PATH, f"{sound}.wav")
                )
                audio_segment = audio_segment.set_channels(AUDIO_CHANNELS)
                audio_segment = audio_segment.set_frame_rate(AUDIO_FRAME_RATE)
                audio_segment = audio_segment.set_sample_width(AUDIO_SAMPLE_WIDTH)
                buffer = io.BytesIO()
                audio_segment.export(buffer, format="raw")
                audio_data = audioop.lin2ulaw(buffer.getvalue(), 1)
                audio_segments[sound] = audio_data
            except Exception as e:
                logger.error(f"Error loading audio segment for {sound}: {e}")
        logger.info(f"Loaded audio segments: {audio_segments.keys()}")
        return audio_segments

    async def play_audio(self, sound: Literal["beep", "ring"]):
        audio_data = self._audio_segments[sound]
        chunk_size = AUDIO_FRAME_RATE // 10
        for i in range(0, len(audio_data), chunk_size):
            chunk = AudioChunk(data=audio_data[i : i + chunk_size])
            self.output_device.consume_nonblocking(
                InterruptibleEvent(
                    payload=chunk,
                    is_interruptible=False,
                ),
            )
        await asyncio.sleep(len(audio_data) / AUDIO_FRAME_RATE)

    async def send_single_message(
        self,
        message: BaseMessage,
        message_tracker: Optional[asyncio.Event] = None,
    ):
        agent_response_event = (
            self.interruptible_event_factory.create_interruptible_agent_response_event(
                AgentResponseMessage(message=message, is_sole_text_chunk=True),
                is_interruptible=False,
                agent_response_tracker=message_tracker,
            )
        )
        self.agent_responses_worker.consume_nonblocking(agent_response_event)
        self.agent_responses_worker.consume_nonblocking(
            self.interruptible_event_factory.create_interruptible_agent_response_event(
                AgentResponseMessage(message=EndOfTurn()),
                is_interruptible=True,
            ),
        )

    def receive_message(self, message: str):
        transcription = Transcription(
            message=message,
            confidence=1.0,
            is_final=True,
        )
        self.transcriptions_worker.consume_nonblocking(transcription)

    def consume_nonblocking(self, item: bytes):
        self.transcriber.send_audio(item)

    def warmup_synthesizer(self):
        self.synthesizer.ready_synthesizer(self._get_synthesizer_chunk_size())

    def mark_last_action_timestamp(self):
        self.last_action_timestamp = time.time()

    async def broadcast_interrupt(self):
        """Stops all inflight events and cancels all workers that are sending output

        Returns true if any events were interrupted - which is used as a flag for the agent (is_interrupt)
        """
        async with self.interrupt_lock:
            num_interrupts = 0
            while True:
                try:
                    interruptible_event = self.interruptible_events.get_nowait()
                    if not interruptible_event.is_interrupted():
                        if interruptible_event.interrupt():
                            logger.debug(
                                f"Interrupting event {type(interruptible_event.payload)} {interruptible_event.payload}",
                            )
                            num_interrupts += 1
                except queue.Empty:
                    break
            self.output_device.interrupt()
            self.agent.cancel_current_task()
            self.agent_responses_worker.cancel_current_task()
            if self.actions_worker:
                self.actions_worker.cancel_current_task()
            return num_interrupts > 0

    def is_interrupt(self, transcription: Transcription):
        return transcription.confidence >= (
            self.transcriber.get_transcriber_config().min_interrupt_confidence or 0
        )

    def _maybe_create_first_chunk_span(self, synthesis_result: SynthesisResult, message: str):
        first_chunk_span: Optional[Span] = None
        if synthesis_result.synthesis_total_span:
            synthesis_result.synthesis_total_span.set_tag("message_length", len(message))
        if synthesis_result.ttft_span:
            synthesis_result.ttft_span.set_tag("message_length", len(message))
            first_chunk_span = sentry_create_span(
                sentry_callable=synthesis_result.ttft_span.start_child,
                op=CustomSentrySpans.SYNTHESIS_GENERATE_FIRST_CHUNK,
            )
        return first_chunk_span

    def _track_first_chunk(self, first_chunk_span: Span, synthesis_result: SynthesisResult):
        complete_span_by_op(CustomSentrySpans.SYNTHESIS_TIME_TO_FIRST_TOKEN)
        first_chunk_span.finish()
        if synthesis_result.ttft_span:
            synthesis_result.ttft_span.finish()
        complete_span_by_op(CustomSentrySpans.LATENCY_OF_CONVERSATION)

    def _get_synthesizer_chunk_size(
        self,
        seconds_per_chunk: float = TEXT_TO_SPEECH_CHUNK_SIZE_SECONDS,
    ):
        return int(
            seconds_per_chunk
            * get_chunk_size_per_second(
                self.synthesizer.get_synthesizer_config().audio_encoding,
                self.synthesizer.get_synthesizer_config().sampling_rate,
            ),
        )

    async def send_speech_to_output(
        self,
        message: str,
        synthesis_result: SynthesisResult,
        stop_event: threading.Event,
        seconds_per_chunk: float,
        transcript_message: Optional[Message] = None,
        started_event: Optional[threading.Event] = None,
    ):
        """
        - Sends the speech chunk by chunk to the output device
          - update the transcript message as chunks come in (transcript_message is always provided for non filler audio utterances)
        - If the stop_event is set, the output is stopped
        - Sets started_event when the first chunk is sent

        Returns the message that was sent up to, and a flag if the message was cut off
        """
        seconds_spoken = 0.0

        def create_on_play_callback(
            chunk_idx: int,
            processed_event: asyncio.Event,
        ):
            def _on_play():
                try:
                    if chunk_idx == 0:
                        if started_event:
                            started_event.set()
                        if first_chunk_span:
                            self._track_first_chunk(first_chunk_span, synthesis_result)

                    nonlocal seconds_spoken

                    self.mark_last_action_timestamp()

                    seconds_spoken += seconds_per_chunk
                    if transcript_message:
                        transcript_message.text = synthesis_result.get_message_up_to(seconds_spoken)

                except Exception as e:
                    logger.error(f"Error in _on_play: {e}")

                processed_event.set()

            return _on_play

        def create_on_interrupt_callback(
            processed_event: asyncio.Event,
        ):
            def _on_interrupt():
                processed_event.set()

            return _on_interrupt

        if self.transcriber.get_transcriber_config().mute_during_speech:
            logger.debug("Muting transcriber")
            self.transcriber.mute()
        logger.debug(f"Start sending speech {message} to output")

        first_chunk_span = self._maybe_create_first_chunk_span(synthesis_result, message)
        audio_chunks: List[AudioChunk] = []
        processed_events: List[asyncio.Event] = []
        interrupted_before_all_chunks_sent = False
        async for chunk_idx, chunk_result in enumerate_async_iter(synthesis_result.chunk_generator):
            logger.debug(f"Generated audio chunk {chunk_idx}: {len(chunk_result.chunk)} bytes")

            if stop_event.is_set():
                logger.debug("Interrupted before all chunks were sent")
                interrupted_before_all_chunks_sent = True
                break
            processed_event = asyncio.Event()
            audio_chunk = AudioChunk(
                data=chunk_result.chunk,
            )
            # register callbacks
            setattr(audio_chunk, "on_play", create_on_play_callback(chunk_idx, processed_event))
            setattr(
                audio_chunk,
                "on_interrupt",
                create_on_interrupt_callback(processed_event),
            )
            # Prevents the case where we send a chunk after the output device has been interrupted
            async with self.interrupt_lock:
                self.output_device.consume_nonblocking(
                    InterruptibleEvent(
                        payload=audio_chunk,
                        is_interruptible=True,
                        interruption_event=stop_event,
                    ),
                )
                logger.debug(f"Sent chunk {chunk_idx} to output device")

            audio_chunks.append(audio_chunk)
            processed_events.append(processed_event)

        logger.debug("Finished sending chunks to the output device")

        if processed_events:
            await processed_events[-1].wait()

        maybe_first_interrupted_audio_chunk = next(
            (
                audio_chunk
                for audio_chunk in audio_chunks
                if audio_chunk.state == ChunkState.INTERRUPTED
            ),
            None,
        )
        cut_off = (
            interrupted_before_all_chunks_sent or maybe_first_interrupted_audio_chunk is not None
        )
        if (
            transcript_message and not cut_off
        ):  # if the audio was not cut off, we can set the transcript message to the full message
            transcript_message.text = synthesis_result.get_message_up_to(None)

        if self.transcriber.get_transcriber_config().mute_during_speech:
            logger.debug("Unmuting transcriber")
            self.transcriber.unmute()
        if transcript_message:
            transcript_message.is_final = not cut_off
        message_sent = transcript_message.text if transcript_message and cut_off else message
        if synthesis_result.synthesis_total_span:
            synthesis_result.synthesis_total_span.finish()
        return message_sent, cut_off

    def mark_terminated(self, bot_disconnect: bool = False):
        self.is_terminated.set()

    async def terminate(self):
        self.mark_terminated()
        await self.broadcast_interrupt()
        self.events_manager.publish_event(
            TranscriptCompleteEvent(
                conversation_id=self.id,
                transcript=self.transcript,
            ),
        )
        if self.check_for_idle_task:
            logger.debug("Terminating check_for_idle Task")
            self.check_for_idle_task.cancel()
        if self.should_run_events_manager and self.events_manager and self.events_task:
            logger.debug("Terminating events Task")
            self.events_task.cancel()
            await self.events_manager.flush()
        else:
            logger.debug(
                "Terminating events: Skipping because events_manager task is managed by the caller"
            )
        logger.debug("Tearing down synthesizer")
        await self.synthesizer.tear_down()
        logger.debug("Terminating agent")
        await self.agent.terminate()
        logger.debug("Terminating output device")
        await self.output_device.terminate()
        logger.debug("Terminating speech transcriber")
        await self.transcriber.terminate()
        logger.debug("Terminating transcriptions worker")
        await self.transcriptions_worker.terminate()
        logger.debug("Terminating final transcriptions worker")
        await self.agent_responses_worker.terminate()
        logger.debug("Terminating synthesis results worker")
        await self.synthesis_results_worker.terminate()
        if self.filler_audio_worker is not None:
            logger.debug("Terminating filler audio worker")
            await self.filler_audio_worker.terminate()
        if self.actions_worker is not None:
            logger.debug("Terminating actions worker")
            await self.actions_worker.terminate()
        logger.debug("Successfully terminated")

    def is_active(self):
        return not self.is_terminated.is_set()

    async def wait_for_termination(self):
        await self.is_terminated.wait()
        logger.debug("Tearing down synthesizer")
        await self.synthesizer.tear_down()
        logger.debug("Terminating agent")
        await self.agent.terminate()
        logger.debug("Terminating output device")
        await self.output_device.terminate()
        logger.debug("Terminating speech transcriber")
        await self.transcriber.terminate()
        logger.debug("Terminating transcriptions worker")
        await self.transcriptions_worker.terminate()
        logger.debug("Terminating final transcriptions worker")
        await self.agent_responses_worker.terminate()
        logger.debug("Terminating synthesis results worker")
        await self.synthesis_results_worker.terminate()
        if self.filler_audio_worker is not None:
            logger.debug("Terminating filler audio worker")
            await self.filler_audio_worker.terminate()
        if self.actions_worker is not None:
            logger.debug("Terminating actions worker")
            await self.actions_worker.terminate()
        logger.debug("Successfully terminated")

    def is_active(self):
        return not self.is_terminated.is_set()

    async def wait_for_termination(self):
        await self.is_terminated.wait()
