import abc
import aiohttp
import asyncio
from functools import partial
from typing import List, Optional

from fastapi import APIRouter, Form, Request, Response, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from loguru import logger
from pydantic.v1 import BaseModel, Field

from vocode.streaming.agent.abstract_factory import AbstractAgentFactory
from vocode.streaming.agent.default_factory import DefaultAgentFactory
from vocode.streaming.models.agent import AgentConfig
from vocode.streaming.models.events import RecordingEvent
from vocode.streaming.models.synthesizer import SynthesizerConfig
from vocode.streaming.models.telephony import (
    IvrConfig,
    IvrDagConfig,
    TwilioCallConfig,
    TwilioConfig,
    VonageCallConfig,
    VonageConfig,
)
from vocode.streaming.models.transcriber import TranscriberConfig
from vocode.streaming.synthesizer.abstract_factory import AbstractSynthesizerFactory
from vocode.streaming.synthesizer.default_factory import DefaultSynthesizerFactory
from vocode.streaming.telephony.client.abstract_telephony_client import AbstractTelephonyClient
from vocode.streaming.telephony.client.twilio_client import TwilioClient
from vocode.streaming.telephony.client.vonage_client import VonageClient
from vocode.streaming.telephony.config_manager.base_config_manager import BaseConfigManager
from vocode.streaming.telephony.config_manager.base_dynamic_call_manager import BaseDynamicCallManager
from vocode.streaming.telephony.server.router.calls import CallsRouter
from vocode.streaming.telephony.templater import get_connection_twiml
from vocode.streaming.transcriber.abstract_factory import AbstractTranscriberFactory
from vocode.streaming.transcriber.default_factory import DefaultTranscriberFactory
from vocode.streaming.utils import create_conversation_id
from vocode.streaming.utils.create_task import asyncio_create_task
from vocode.streaming.utils.events_manager import EventsManager
from vocode.streaming.utils.async_requester import AsyncRequestor


class AbstractInboundCallConfig(BaseModel, abc.ABC):
    url: str
    agent_config: AgentConfig
    transcriber_config: Optional[TranscriberConfig] = None
    synthesizer_config: Optional[SynthesizerConfig] = None
    auth_token: Optional[str] = None
    verify_token: Optional[bool] = False
    ivr_config: Optional[IvrConfig] = None
    ivr_dag: Optional[IvrDagConfig] = None
    dynamic_call: Optional[bool] = None


class TwilioInboundCallConfig(AbstractInboundCallConfig):
    twilio_config: TwilioConfig


class VonageInboundCallConfig(AbstractInboundCallConfig):
    vonage_config: VonageConfig


class VonageAnswerRequest(BaseModel):
    to: str
    from_: str = Field(..., alias="from")
    uuid: str

class TwilioBadRequestException(ValueError):
    pass

class TwilioException(ValueError):
    pass


class TelephonyServer:
    def __init__(
        self,
        base_url: str,
        config_manager: BaseConfigManager,
        inbound_call_configs: List[AbstractInboundCallConfig] = [],
        transcriber_factory: AbstractTranscriberFactory = DefaultTranscriberFactory(),
        agent_factory: AbstractAgentFactory = DefaultAgentFactory(),
        synthesizer_factory: AbstractSynthesizerFactory = DefaultSynthesizerFactory(),
        events_manager: Optional[EventsManager] = None,
        dynamic_call_manager: Optional[BaseDynamicCallManager] = None,
    ):
        self.base_url = base_url
        self.router = APIRouter()
        self.config_manager = config_manager
        self.events_manager = events_manager
        self.dynamic_call_manager = dynamic_call_manager
        self.security = HTTPBearer(auto_error=False)
        self.router.include_router(
            CallsRouter(
                base_url=base_url,
                config_manager=self.config_manager,
                transcriber_factory=transcriber_factory,
                agent_factory=agent_factory,
                synthesizer_factory=synthesizer_factory,
                events_manager=self.events_manager,
            ).get_router()
        )
        for config in inbound_call_configs:
            self.router.add_api_route(
                config.url,
                self.create_inbound_route(inbound_call_config=config),
                methods=["POST"],
            )
        # vonage requires an events endpoint
        self.router.add_api_route("/events", self.events, methods=["GET", "POST"])
        logger.info(f"Set up events endpoint at https://{self.base_url}/events")

        self.router.add_api_route(
            "/recordings/{conversation_id}", self.recordings, methods=["GET", "POST"]
        )
        logger.info(
            f"Set up recordings endpoint at https://{self.base_url}/recordings/{{conversation_id}}"
        )

    def events(self, request: Request):
        return Response()

    async def recordings(self, request: Request, conversation_id: str):
        recording_url = (await request.json())["recording_url"]
        if self.events_manager is not None and recording_url is not None:
            self.events_manager.publish_event(
                RecordingEvent(recording_url=recording_url, conversation_id=conversation_id)
            )
        return Response()

    def create_inbound_route(
        self,
        inbound_call_config: AbstractInboundCallConfig,
    ):
        if inbound_call_config.verify_token and inbound_call_config.auth_token is None:
            raise ValueError("Auth token is required to verify tokens")
        
        async def verify_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(self.security)):
            if not inbound_call_config.verify_token:
                return None
            if not credentials or credentials.credentials != inbound_call_config.auth_token:
                raise HTTPException(status_code=401, detail="Invalid or missing token")
            return credentials.credentials

        async def twilio_route(
            twilio_config: TwilioConfig,
            twilio_sid: str = Form(alias="CallSid"),
            twilio_from: str = Form(alias="From"),
            twilio_to: str = Form(alias="To"),
            _: str = Depends(verify_token),
        ) -> Response:
            conversation_id = create_conversation_id()
            if self.dynamic_call_manager and inbound_call_config.dynamic_call:
                agent_config = await self.dynamic_call_manager.create_call(
                    twilio_sid,
                    twilio_from,
                    twilio_to, 
                    conversation_id
                )
                asyncio_create_task(self._start_recording(twilio_config, twilio_sid))
            else:
                agent_config = inbound_call_config.agent_config
            call_config = TwilioCallConfig(
                transcriber_config=inbound_call_config.transcriber_config
                or TwilioCallConfig.default_transcriber_config(),
                agent_config=agent_config,
                synthesizer_config=inbound_call_config.synthesizer_config
                or TwilioCallConfig.default_synthesizer_config(),
                twilio_config=twilio_config,
                twilio_sid=twilio_sid,
                from_phone=twilio_from,
                to_phone=twilio_to,
                direction="inbound",
                ivr_config=inbound_call_config.ivr_config,
                ivr_dag=inbound_call_config.ivr_dag,
            )
            await self.config_manager.save_config(conversation_id, call_config)
            return get_connection_twiml(base_url=self.base_url, call_id=conversation_id)

        async def vonage_route(vonage_config: VonageConfig, request: Request):
            vonage_answer_request = VonageAnswerRequest.parse_obj(await request.json())
            call_config = VonageCallConfig(
                transcriber_config=inbound_call_config.transcriber_config
                or VonageCallConfig.default_transcriber_config(),
                agent_config=inbound_call_config.agent_config,
                synthesizer_config=inbound_call_config.synthesizer_config
                or VonageCallConfig.default_synthesizer_config(),
                vonage_config=vonage_config,
                vonage_uuid=vonage_answer_request.uuid,
                to_phone=vonage_answer_request.from_,
                from_phone=vonage_answer_request.to,
                direction="inbound",
            )
            conversation_id = create_conversation_id()
            await self.config_manager.save_config(conversation_id, call_config)
            vonage_client = VonageClient(
                base_url=self.base_url,
                maybe_vonage_config=vonage_config,
                record_calls=vonage_config.record,
            )
            return vonage_client.create_call_ncco(
                conversation_id=conversation_id,
                record=vonage_config.record,
            )

        if isinstance(inbound_call_config, TwilioInboundCallConfig):
            logger.info(
                f"Set up inbound call TwiML at https://{self.base_url}{inbound_call_config.url}"
            )
            return partial(twilio_route, inbound_call_config.twilio_config)
        elif isinstance(inbound_call_config, VonageInboundCallConfig):
            logger.info(
                f"Set up inbound call NCCO at https://{self.base_url}{inbound_call_config.url}"
            )
            return partial(vonage_route, inbound_call_config.vonage_config)
        else:
            raise ValueError(f"Unknown inbound call config type {type(inbound_call_config)}")

    async def end_outbound_call(self, conversation_id: str):
        # TODO validation via twilio_client
        call_config = await self.config_manager.get_config(conversation_id)
        if not call_config:
            raise ValueError(f"Could not find call config for {conversation_id}")
        telephony_client: AbstractTelephonyClient
        if isinstance(call_config, TwilioCallConfig):
            telephony_client = TwilioClient(
                base_url=self.base_url, maybe_twilio_config=call_config.twilio_config
            )
            await telephony_client.end_call(call_config.twilio_sid)
        elif isinstance(call_config, VonageCallConfig):
            telephony_client = VonageClient(
                base_url=self.base_url, maybe_vonage_config=call_config.vonage_config
            )
            await telephony_client.end_call(call_config.vonage_uuid)
        return {"id": conversation_id}

    def get_router(self) -> APIRouter:
        return self.router
    
    async def _start_recording(self, twilio_config: TwilioConfig, twilio_sid: str):
        logger.info(f"Starting recording for {twilio_sid}")
        await asyncio.sleep(1) # TODO: run this in a loop until recording is started
        auth = aiohttp.BasicAuth(
            login=twilio_config.account_sid,
            password=twilio_config.auth_token,
        )
        # https://help.twilio.com/articles/360010317333-Recording-Incoming-Twilio-Voice-Calls
        async with AsyncRequestor().get_session().post(
            f"https://api.twilio.com/2010-04-01/Accounts/{twilio_config.account_sid}/Calls/{twilio_sid}/Recordings.json",
            auth=auth,
            data={
                "RecordingStatusCallback": f"https://{self.base_url}/v1/recordings/webhook",
                "RecordingChannels": "dual",
                "RecordingStatusCallbackEvent": "completed",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as response:
            if not response.ok:
                if response.status == 400:
                    logger.warning(
                        f"Failed to create recording: {response.status} {response.reason} {await response.json()}"
                    )
                    raise TwilioBadRequestException(
                        "Telephony provider rejected recording."
                    )
                else:
                    raise TwilioException(
                        f"Twilio failed to create recording: {response.status} {response.reason}"
                    )
            response = await response.json()
            logger.info(f"Recording created: {response}")
