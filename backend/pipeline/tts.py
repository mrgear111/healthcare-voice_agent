import os
import asyncio
import logging
from deepgram import AsyncDeepgramClient
from deepgram.core.events import EventType
from deepgram.speak.v1.types.speak_v1text import SpeakV1Text

logger = logging.getLogger(__name__)

class DeepgramTTSHandler:
    def __init__(self):
        self.api_key = os.getenv("DEEPGRAM_API_KEY")
        self.client = AsyncDeepgramClient(api_key=self.api_key)
        self.model = "aura-asteria-en" # Default clear female voice
        self._ctx = None
        self.dg_connection = None

    async def stream_audio(self, text_iterator, websocket, language="en", on_audio_start=None):
        """
        Streams audio chunks from Deepgram Aura to the client WebSocket.
        """
        try:
            self.set_language(language)
            self._ctx = self.client.speak.v1.connect(
                model=self.model,
                encoding="linear16",
                sample_rate="16000",
            )
            self.dg_connection = await self._ctx.__aenter__()
            
            self._audio_started = False

            # Start background listener for the audio chunks
            async def on_message(message, **kwargs):
                if isinstance(message, bytes):
                    if not self._audio_started:
                        self._audio_started = True
                        if on_audio_start:
                            await on_audio_start()
                    if websocket:
                        await websocket.send_bytes(message)
                # Metadata can be ignored for basic streaming

            async def on_error(error, **kwargs):
                logger.error(f"Deepgram TTS Error: {error}")

            self.dg_connection.on(EventType.MESSAGE, on_message)
            self.dg_connection.on(EventType.ERROR, on_error)
            
            # Start listening task
            asyncio.create_task(self.dg_connection.start_listening())

            # Send text chunks from the iterator
            async for text in text_iterator:
                if text:
                    await self.dg_connection.send_text(SpeakV1Text(text=text))
            
            # Deepgram TTS needs a Flush or Close to finish streaming
            await self.dg_connection.send_flush()
            
            # Wait a bit for remaining audio
            await asyncio.sleep(0.5)
            
        except Exception as e:
            logger.error(f"Deepgram TTS streaming error: {e}")
        finally:
            if self._ctx:
                await self._ctx.__aexit__(None, None, None)
                self.dg_connection = None
                self._ctx = None

    def set_language(self, language_code):
        # Deepgram Aura models are language-specific
        if language_code == "en":
            self.model = "aura-asteria-en"
        elif language_code == "hi":
            # Check availability or fallback
            pass
        elif language_code == "ta":
            # Check availability or fallback
            pass
