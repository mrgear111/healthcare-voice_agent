import os
import asyncio
import logging
import base64

try:
    from sarvamai import AsyncSarvamAI, AudioOutput, EventResponse, ErrorResponse
except ImportError:  # pragma: no cover - handled at runtime
    AsyncSarvamAI = None
    AudioOutput = object
    EventResponse = object
    ErrorResponse = object

logger = logging.getLogger(__name__)

class SarvamTTSHandler:
    def __init__(self):
        self.api_key = os.getenv("SARVAM_API_KEY")
        self.client = AsyncSarvamAI(api_subscription_key=self.api_key) if (AsyncSarvamAI and self.api_key) else None
        self.model = "bulbul:v3"
        self.target_language_code = "en-IN"
        self.speaker = "shubh"

    async def stream_audio(
        self,
        text_iterator,
        websocket,
        language="en",
        on_audio_start=None,
        on_audio_end=None,
    ):
        """
        Streams PCM audio chunks from Sarvam TTS to the client WebSocket.
        """
        if AsyncSarvamAI is None:
            logger.error("sarvamai package is not installed. Install with: pip install sarvamai")
            return
        if not self.api_key:
            logger.error("Missing SARVAM_API_KEY in environment")
            return

        try:
            self.set_language(language)
            async with self.client.text_to_speech_streaming.connect(
                model=self.model,
                send_completion_event=True,
            ) as ws:
                await ws.configure(
                    target_language_code=self.target_language_code,
                    speaker=self.speaker,
                    output_audio_codec="wav",
                    speech_sample_rate=16000,
                )

                audio_started = False

                async def send_text() -> None:
                    async for text in text_iterator:
                        if text:
                            await ws.convert(text)
                    await ws.flush()

                sender_task = asyncio.create_task(send_text())

                async for message in ws:
                    if isinstance(message, AudioOutput):
                        audio_b64 = getattr(message.data, "audio", "")
                        if not audio_b64:
                            continue

                        if not audio_started:
                            audio_started = True
                            if on_audio_start:
                                await on_audio_start()

                        if websocket:
                            await websocket.send_bytes(base64.b64decode(audio_b64))
                    elif isinstance(message, EventResponse):
                        event_type = getattr(message.data, "event_type", "")
                        if event_type == "final":
                            break
                    elif isinstance(message, ErrorResponse):
                        logger.error("Sarvam TTS stream error: %s", message.data)
                        break

                await sender_task
            
        except Exception as e:
            logger.error(f"Sarvam TTS streaming error: {e}", exc_info=True)
        finally:
            if on_audio_end:
                try:
                    await on_audio_end()
                except Exception:
                    pass

    def set_language(self, language_code):
        language_map = {
            "en": "en-IN",
            "hi": "hi-IN",
            "ta": "ta-IN",
        }
        self.target_language_code = language_map.get(language_code, "en-IN")

        # Keep a documented speaker value to avoid invalid-speaker failures.
        self.speaker = "shubh"
