import asyncio
import json
import os
import logging
from websockets.asyncio.client import connect as ws_connect

logger = logging.getLogger(__name__)

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"

class DeepgramHandler:
    def __init__(self, callback):
        self.api_key = os.getenv("DEEPGRAM_API_KEY")
        self.callback = callback
        self.ws = None
        self._listen_task = None
        self._running = False
        self._send_queue: asyncio.Queue = asyncio.Queue()

    async def start(self, language="en-US", sample_rate=16000):
        params = (
            f"model=nova-2"
            f"&language={language}"
            f"&smart_format=true"
            f"&interim_results=true"
            f"&encoding=linear16"
            f"&channels=1"
            f"&sample_rate={sample_rate}"
            f"&endpointing=300"
            f"&utterance_end_ms=1000"
            f"&vad_events=true"
        )
        url = f"{DEEPGRAM_WS_URL}?{params}"
        headers = {"Authorization": f"Token {self.api_key}"}

        try:
            self.ws = await ws_connect(url, additional_headers=headers)
            self._running = True
            logger.info(f"✅ Deepgram WebSocket connected (lang={language})")

            # Start sender and receiver tasks
            self._listen_task = asyncio.create_task(self._recv_loop())
            self._send_task = asyncio.create_task(self._send_loop())
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Deepgram: {e}", exc_info=True)
            return False

    async def _recv_loop(self):
        """Dedicated receive loop — runs independently of sends."""
        logger.info("🔊 Deepgram receive loop started")
        try:
            async for raw_message in self.ws:
                if isinstance(raw_message, bytes):
                    continue  # skip binary keepalives
                try:
                    data = json.loads(raw_message)
                    msg_type = data.get("type", "")

                    if msg_type == "Results":
                        logger.debug(f"DG Results Keys: {list(data.keys())}")
                        
                        # Aggressive search for alternatives
                        alternatives = None
                        if "channel" in data:
                            alternatives = data["channel"].get("alternatives")
                        elif "results" in data:
                            channels = data["results"].get("channels", [])
                            if channels:
                                alternatives = channels[0].get("alternatives")
                        
                        if not alternatives and "alternatives" in data:
                            alternatives = data["alternatives"]
                            
                        if alternatives:
                            alt = alternatives[0]
                            transcript = alt.get("transcript", "")
                            confidence = alt.get("confidence", 0.0)
                            is_final = data.get("is_final", False)
                            speech_final = data.get("speech_final", False)
                            if transcript:
                                logger.info(f"✅ DG STT: '{transcript}' (final={is_final}, speech_final={speech_final}, conf={confidence:.2f})")
                                await self.callback(transcript, speech_final or is_final, confidence)
                            else:
                                pass # empty transcript is common in interim results
                        else:
                            logger.debug(f"Could not find alternatives in DG Result. Keys: {list(data.keys())}")
                    elif msg_type == "Metadata":
                        logger.debug(f"DG Metadata received")
                    elif msg_type == "SpeechStarted":
                        logger.debug("DG: Speech started")
                        await self.callback("", False, speech_started=True)
                    else:
                        logger.debug(f"DG msg: {msg_type}")
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    logger.error(f"Error in recv_loop: {e}", exc_info=True)
        except Exception as e:
            logger.warning(f"Deepgram recv loop ended: {e}")
        finally:
            self._running = False
            logger.info("Deepgram recv loop stopped")

    async def _send_loop(self):
        """Dedicated send loop — drains the queue."""
        logger.info("📤 Deepgram send loop started")
        try:
            while self._running:
                data = await asyncio.wait_for(self._send_queue.get(), timeout=1.0)
                if data is None:
                    break
                if self.ws and self._running:
                    await self.ws.send(data)
        except asyncio.TimeoutError:
            pass  # normal, loop restarts
        except Exception as e:
            logger.warning(f"Deepgram send loop ended: {e}")

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
                await self.ws.close()
            except Exception:
                pass
        self.ws = None
        logger.info("Deepgram handler stopped")
