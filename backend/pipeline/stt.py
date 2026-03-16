import asyncio
import base64
import io
import json
import os
import logging
import wave
from array import array

try:
    from sarvamai import AsyncSarvamAI
except ImportError:  # pragma: no cover - handled at runtime
    AsyncSarvamAI = None

logger = logging.getLogger(__name__)

class SarvamHandler:
    def __init__(self, callback):
        self.api_key = os.getenv("SARVAM_API_KEY")
        self.callback = callback
        self.client = AsyncSarvamAI(api_subscription_key=self.api_key) if (AsyncSarvamAI and self.api_key) else None
        self._ctx = None
        self.ws = None
        self._listen_task = None
        self._running = False
        self.input_sample_rate = 16000
        self.sample_rate = 16000
        self.chunk_ms = 200
        self._buffer = bytearray()
        self._send_queue: asyncio.Queue = asyncio.Queue()

    async def start(self, language="en-US", sample_rate=16000):
        if AsyncSarvamAI is None:
            logger.error("sarvamai package is not installed. Install with: pip install sarvamai")
            return False
        if not self.api_key:
            logger.error("Missing SARVAM_API_KEY in environment")
            return False

        try:
            self.input_sample_rate = sample_rate
            # SDK audio messages currently require WAV payloads; keep stream at 16kHz.
            self.sample_rate = 16000
            if self.sample_rate != self.input_sample_rate:
                logger.info(
                    "Resampling microphone audio from %sHz to %sHz for Sarvam STT",
                    self.input_sample_rate,
                    self.sample_rate,
                )
            self._ctx = self.client.speech_to_text_streaming.connect(
                model="saaras:v3",
                mode="transcribe",
                language_code=self._map_language(language),
                sample_rate=self.sample_rate,
                input_audio_codec="pcm_s16le",
                high_vad_sensitivity=True,
                vad_signals=True,
                flush_signal=True,
            )
            self.ws = await self._ctx.__aenter__()
            self._running = True
            logger.info(
                "Sarvam STT streaming connected (lang=%s, input_rate=%s, stream_rate=%s)",
                language,
                self.input_sample_rate,
                self.sample_rate,
            )

            # Start sender and receiver tasks
            self._listen_task = asyncio.create_task(self._recv_loop())
            self._send_task = asyncio.create_task(self._send_loop())
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Sarvam STT: {e}", exc_info=True)
            return False

    @staticmethod
    def _map_language(language: str) -> str:
        mapping = {
            "en": "en-IN",
            "en-US": "en-IN",
            "en-IN": "en-IN",
            "hi": "hi-IN",
            "hi-IN": "hi-IN",
            "ta": "ta-IN",
            "ta-IN": "ta-IN",
        }
        return mapping.get(language, "en-IN")

    async def _recv_loop(self):
        """Dedicated receive loop — runs independently of sends."""
        logger.info("Sarvam STT receive loop started")
        try:
            async for message in self.ws:
                try:
                    if isinstance(message, dict):
                        data = message
                    elif isinstance(message, str):
                        data = json.loads(message)
                    elif hasattr(message, "model_dump"):
                        data = message.model_dump()
                    elif hasattr(message, "dict"):
                        data = message.dict()
                    else:
                        logger.debug("Unknown STT message type: %s", type(message))
                        continue

                    msg_type = data.get("type", "")
                    payload = data.get("data") or {}

                    if msg_type == "events":
                        event_type = payload.get("event_type") or payload.get("signal_type")
                        logger.debug("Sarvam event: %s", event_type)
                    elif msg_type == "data":
                        transcript = (payload.get("transcript") or "").strip()
                        confidence = float(payload.get("language_probability") or 0.0)
                        is_final = True
                        if transcript:
                            logger.info(
                                "Sarvam STT: '%s' (final=%s, conf=%.2f)",
                                transcript,
                                is_final,
                                confidence,
                            )
                            await self.callback(transcript, is_final, confidence)
                    elif msg_type == "error":
                        logger.warning("Sarvam STT error payload: %s", payload)
                    else:
                        logger.debug(f"Sarvam STT message: {msg_type}")
                except json.JSONDecodeError:
                    logger.debug("Skipped non-JSON STT message")
                except Exception as e:
                    logger.error(f"Error in recv_loop: {e}", exc_info=True)
        except Exception as e:
            logger.warning(f"Sarvam STT recv loop ended: {e}")
        finally:
            self._running = False
            logger.info("Sarvam STT recv loop stopped")

    async def _send_loop(self):
        """Dedicated send loop — drains the queue."""
        logger.info("Sarvam STT send loop started")
        min_chunk_bytes = max(2, int(self.input_sample_rate * 2 * (self.chunk_ms / 1000.0)))

        while self._running:
            try:
                data = await asyncio.wait_for(self._send_queue.get(), timeout=1.0)
                if data is None:
                    break

                self._buffer.extend(data)

                if self.ws and self._running:
                    while len(self._buffer) >= min_chunk_bytes:
                        frame = bytes(self._buffer[:min_chunk_bytes])
                        del self._buffer[:min_chunk_bytes]
                        await self._send_frame(frame)
            except asyncio.TimeoutError:
                if self.ws and self._running and self._buffer:
                    frame = bytes(self._buffer)
                    self._buffer.clear()
                    await self._send_frame(frame)
                continue
            except Exception as e:
                logger.warning(f"Sarvam STT send loop ended: {e}")

    async def _send_frame(self, pcm_input: bytes) -> None:
        pcm16 = self._resample_pcm16le(
            pcm_input,
            self.input_sample_rate,
            self.sample_rate,
        )
        wav_bytes = self._pcm16le_to_wav_bytes(pcm16, self.sample_rate)
        await self.ws.transcribe(
            audio=base64.b64encode(wav_bytes).decode("utf-8"),
            encoding="audio/wav",
            sample_rate=self.sample_rate,
        )

    @staticmethod
    def _resample_pcm16le(data: bytes, input_rate: int, output_rate: int) -> bytes:
        if input_rate <= 0 or output_rate <= 0 or input_rate == output_rate:
            return data

        source = array("h")
        source.frombytes(data)
        if not source:
            return data

        out_len = max(1, int(len(source) * output_rate / input_rate))
        resampled = array("h")

        for i in range(out_len):
            pos = (i * input_rate) / output_rate
            lo = int(pos)
            hi = min(lo + 1, len(source) - 1)
            frac = pos - lo
            value = int(source[lo] * (1.0 - frac) + source[hi] * frac)
            resampled.append(max(-32768, min(32767, value)))

        return resampled.tobytes()

    @staticmethod
    def _pcm16le_to_wav_bytes(pcm_data: bytes, sample_rate: int) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(pcm_data)
        return buffer.getvalue()

    async def send_data(self, data: bytes):
        """Queue audio data for sending — non-blocking."""
        if self._running:
            await self._send_queue.put(data)

    async def stop(self):
        self._running = False
        # Signal send loop to stop
        await self._send_queue.put(None)
        # Cancel tasks
        for task in [getattr(self, '_listen_task', None), getattr(self, '_send_task', None)]:
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if self.ws:
            try:
                if self._buffer:
                    frame = bytes(self._buffer)
                    self._buffer.clear()
                    await self._send_frame(frame)
                await self.ws.flush()
            except Exception:
                pass

        if self._ctx:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                pass
        self.ws = None
        self._ctx = None
        logger.info("Sarvam STT handler stopped")
