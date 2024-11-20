import asyncio
import copy
import websockets
import base64
import json
import aiohttp
import os
import traceback
from collections import deque

from bolna.memory.cache.inmemory_scalar_cache import InmemoryScalarCache
from .base_synthesizer import BaseSynthesizer
from bolna.helpers.logger_config import configure_logger
from bolna.helpers.utils import convert_audio_to_wav, create_ws_data_packet, pcm_to_wav_bytes, resample


logger = configure_logger(__name__)


class ElevenlabsSynthesizer(BaseSynthesizer):
    def __init__(self, voice, voice_id, model="eleven_turbo_v2_5", audio_format="mp3", sampling_rate="16000",
                 stream=False, buffer_size=400, temperature=0.9, similarity_boost=0.5, synthesizer_key=None,
                 caching=True, **kwargs):
        super().__init__(stream)
        self.api_key = os.environ["ELEVENLABS_API_KEY"] if synthesizer_key is None else synthesizer_key
        self.voice = voice_id
        self.model = model
        self.stream = True  # Issue with elevenlabs streaming that we need to always send the text quickly
        self.sampling_rate = sampling_rate
        self.audio_format = "mp3"
        self.use_mulaw = kwargs.get("use_mulaw", False)
        self.ws_url = f"wss://api.elevenlabs.io/v1/text-to-speech/{self.voice}/stream-input?model_id={self.model}&output_format=ulaw_8000&inactivity_timeout=60"
        self.api_url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice}?optimize_streaming_latency=2&output_format="
        self.first_chunk_generated = False
        self.last_text_sent = False
        self.text_queue = deque()
        self.meta_info = None
        self.temperature = 0.8
        self.similarity_boost = similarity_boost
        self.caching = caching
        if self.caching:
            self.cache = InmemoryScalarCache()
        self.synthesized_characters = 0
        self.previous_request_ids = []
        self.websocket_holder = {"websocket": None}
        self.sender_task = None

    # Ensuring we only do wav output for now
    def get_format(self, format, sampling_rate):
        # Eleven labs only allow mp3_44100_64, mp3_44100_96, mp3_44100_128, mp3_44100_192, pcm_16000, pcm_22050,
        # pcm_24000, ulaw_8000
        if self.use_mulaw:
            return "ulaw_8000"
        return f"mp3_44100_128"

    def get_engine(self):
        return self.model

    async def sender(self, text, end_of_llm_stream=False):
        try:
            # Ensure the WebSocket connection is established
            while self.websocket_holder["websocket"] is None or self.websocket_holder["websocket"].closed:
                logger.info("Waiting for elevenlabs ws connection to be established...")
                await asyncio.sleep(1)

            if text != "":
                for text_chunk in self.text_chunker(text):
                    logger.info(f"Sending text_chunk: {text_chunk}")
                    try:
                        await self.websocket_holder["websocket"].send(json.dumps({"text": text_chunk}))
                    except Exception as e:
                        logger.info(f"Error sending chunk: {e}")
                        return

            # If end_of_llm_stream is True, mark the last chunk and send an empty message
            if end_of_llm_stream:
                self.last_text_sent = True

            # Send the end-of-stream signal with an empty string as text
            try:
                await self.websocket_holder["websocket"].send(json.dumps({"text": "", "flush": True}))
            except Exception as e:
                logger.info(f"Error sending end-of-stream signal: {e}")

        except asyncio.CancelledError:
            logger.info("Sender task was cancelled.")
        except Exception as e:
            logger.error(f"Unexpected error in sender: {e}")

    async def receiver(self):
        while not asyncio.current_task().cancelled():
            try:
                if self.websocket_holder["websocket"] is None or self.websocket_holder["websocket"].closed:
                    logger.info("WebSocket is not connected, skipping receive.")
                    await asyncio.sleep(5)
                    continue

                response = await self.websocket_holder["websocket"].recv()
                data = json.loads(response)
                logger.info("response for isFinal: {}".format(data.get('isFinal', False)))
                if "audio" in data and data["audio"]:
                    chunk = base64.b64decode(data["audio"])
                    #if len(chunk) % 2 == 1:
                    #   chunk += b'\x00'
                    # @TODO make it better - for example sample rate changing for mp3 and other formats  
                    yield chunk

                    if "isFinal" in data and data["isFinal"]:
                        yield b'\x00'
                else:
                    logger.info("No audio data in the response")

            except asyncio.CancelledError:
                logger.info("Receiver task was cancelled.")
                break
            except Exception as e:
                logger.error(f"Error in receiver: {e}")
                await asyncio.sleep(1)  # Avoid busy waiting

    def get_synthesized_characters(self):
        return self.synthesized_characters

    # Currently we are only supporting wav output but soon we will incorporate conver
    async def generate(self):
        try:
            async for message in self.receiver():
                logger.info(f"Received message from server")

                if len(self.text_queue) > 0:
                    self.meta_info = self.text_queue.popleft()
                audio = ""

                if self.use_mulaw:
                    self.meta_info['format'] = 'mulaw'
                    audio = message
                else:
                    self.meta_info['format'] = "wav"
                    audio = resample(convert_audio_to_wav(message, source_format="mp3"), int(self.sampling_rate),
                                         format="wav")

                yield create_ws_data_packet(audio, self.meta_info)
                if not self.first_chunk_generated:
                    self.meta_info["is_first_chunk"] = True
                    self.first_chunk_generated = True

                if self.last_text_sent:
                    # Reset the last_text_sent and first_chunk converted to reset synth latency
                    self.first_chunk_generated = False
                    self.last_text_sent = True

                if message == b'\x00':
                    logger.info("received null byte and hence end of stream")
                    self.meta_info["end_of_synthesizer_stream"] = True
                    yield create_ws_data_packet(resample(message, int(self.sampling_rate)), self.meta_info)
                    self.first_chunk_generated = False

        except Exception as e:
            traceback.print_exc()
            logger.info(f"Error in eleven labs generate {e}")

    def supports_websocket(self):
        return True

    async def establish_connection(self):
        try:
            websocket = await websockets.connect(self.ws_url)
            bos_message = {
                "text": " ",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.8
                },
                "xi_api_key": self.api_key
            }
            await websocket.send(json.dumps(bos_message))
            logger.info(f"Connected to {self.ws_url}")
            return websocket
        except Exception as e:
            logger.info(f"Failed to connect: {e}")
            return None

    async def monitor_connection(self):
        # Periodically check if the connection is still alive
        while True:
            if self.websocket_holder["websocket"] is None or self.websocket_holder["websocket"].closed:
                logger.info("Re-establishing elevenlabs connection...")
                self.websocket_holder["websocket"] = await self.establish_connection()
            await asyncio.sleep(50)

    async def get_sender_task(self):
        return self.sender_task

    async def push(self, message):
        logger.info(f"Pushed message to internal queue {message}")
        if self.stream:
            meta_info, text = message.get("meta_info"), message.get("data")
            self.synthesized_characters += len(text) if text is not None else 0
            end_of_llm_stream = "end_of_llm_stream" in meta_info and meta_info["end_of_llm_stream"]
            logger.info(f"end_of_llm_stream: {end_of_llm_stream}")
            self.meta_info = copy.deepcopy(meta_info)
            meta_info["text"] = text
            self.sender_task = asyncio.create_task(self.sender(text, end_of_llm_stream))
            self.text_queue.append(meta_info)
        else:
            self.internal_queue.put_nowait(message)

    async def cleanup(self):
        logger.info("cleaning elevenlabs synthesizer tasks")
        if self.sender_task and not self.sender_task.done():
            self.sender_task.cancel()
            try:
                await self.sender_task
            except asyncio.CancelledError:
                logger.info("Sender task was successfully cancelled during WebSocket cleanup.")

        if self.websocket_holder["websocket"]:
            await self.websocket_holder["websocket"].close()
        self.websocket_holder["websocket"] = None
        logger.info("WebSocket connection closed.")
