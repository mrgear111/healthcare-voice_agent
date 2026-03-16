import asyncio
import json
import os
from dotenv import load_dotenv
load_dotenv()
import logging
import random
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pipeline.stt import SarvamHandler
from pipeline.llm import LLMService
from pipeline.tts import SarvamTTSHandler
from services.appointment import AppointmentService

from services.tools import get_appointment_tools
from memory.manager import MemoryManager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
appointment_service = AppointmentService()
llm_service = LLMService()
tts_handler = SarvamTTSHandler()
memory_manager = MemoryManager()
active_sessions = {} # Track {session_id: task} for interruption
session_runtime = {} # Track per-session speech state

async def interrupt_session(session_id, websocket):
    interrupted = False
    if session_id in active_sessions:
        task = active_sessions[session_id]
        if not task.done():
            logger.info(f"🚫 Interrupting session {session_id}")
            task.cancel()
            interrupted = True
            try:
                await task
            except asyncio.CancelledError:
                pass
        active_sessions.pop(session_id, None)
    
    state = session_runtime.get(session_id, {})
    pending_task = state.get("pending_llm_task")
    if pending_task and not pending_task.done():
        pending_task.cancel()
    state["pending_llm_task"] = None
    state["pending_user_text"] = ""
    state["pending_confidence"] = 0.0
    state["pending_start_time"] = 0.0
    state["assistant_speaking"] = False
    state["assistant_speaking_until"] = 0.0
    state["barge_in_armed"] = False
    # Only clear audio if we actually stopped an ongoing process
    if interrupted and websocket:
        await websocket.send_json({"type": "clear_audio"})

async def stream_to_tts(
    text_generator,
    websocket,
    language="en",
    on_audio_start=None,
    on_audio_end=None,
):
    """ Helper to pipe text stream to tts and then to websocket """
    await tts_handler.stream_audio(
        text_generator,
        websocket,
        language=language,
        on_audio_start=on_audio_start,
        on_audio_end=on_audio_end,
    )

async def llm_callback(text, is_final=False, websocket=None, session_id="default", language="en", confidence=0.0, start_time=None):
    logger.debug(f"llm_callback trace: text='{text}', is_final={is_final}, conf={confidence}")
    if not is_final and text is not None:
        return

    logger.info(f"Executing LLM for: {text} (Session: {session_id}, Language: {language})")
    state = session_runtime.setdefault(session_id, {})
    state["llm_in_flight"] = True
    try:
        # Start timing for metrics
        import time
        t0 = start_time or time.time()
        
        async def on_audio_start():
            state["assistant_speaking"] = True
            state["barge_in_armed"] = True
            latency = int((time.time() - t0) * 1000)
            logger.info(f"📊 Latency Metric: {latency}ms")
            if websocket:
                await websocket.send_json({
                    "type": "metrics",
                    "latency": latency,
                    "confidence": confidence
                })

        async def on_audio_end():
            # Small cooldown avoids re-transcribing echoed assistant audio tail.
            state["assistant_speaking"] = False
            state["barge_in_armed"] = False
            state["assistant_speaking_until"] = time.time() + 0.9

        # 1. Fetch persistent history (Requirement: Awareness of patient context)
        conversation_history = memory_manager.get_session(session_id)
        if not conversation_history:
            system_prompt = llm_service.get_system_prompt(language=language)
            conversation_history = [{"role": "system", "content": system_prompt}]
        
        conversation_history.append({"role": "user", "content": text})
        tools = get_appointment_tools()
        
        # 2. Start LLM Stream with bounded retry for transient overload.
        response_stream = None
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                response_stream = await llm_service.get_response(conversation_history, tools=tools)
                break
            except Exception as err:
                overloaded = "overloaded" in str(err).lower()
                if overloaded and attempt < max_retries:
                    backoff_s = (0.6 * (2 ** attempt)) + random.uniform(0.0, 0.25)
                    logger.warning(
                        "Anthropic overloaded; retrying in %.2fs (attempt %d/%d)",
                        backoff_s,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(backoff_s)
                    continue
                raise
        
        # 3. Setup Async Queue for TTS piping
        text_queue = asyncio.Queue()
        async def text_iterator():
            while True:
                val = await text_queue.get()
                if val is None: break
                yield val

        # Start TTS streaming in background (Requirement: Standard Indian language support)
        tts_task = asyncio.create_task(
            stream_to_tts(
                text_iterator(),
                websocket,
                language=language,
                on_audio_start=on_audio_start,
                on_audio_end=on_audio_end,
            )
        )

        full_response = ""
        buffer = ""
        tool_use = None
        tool_input_json = ""
        
        async for event in response_stream:
            if event.type == "content_block_delta":
                if event.delta.type == "text_delta":
                    content = event.delta.text
                    full_response += content
                    buffer += content
                    
                    # Flush by punctuation or chunk size so TTS starts quickly.
                    if any(p in content for p in [".", "?", "!", "\n"]) or len(buffer) >= 120:
                        await text_queue.put(buffer)
                        buffer = ""
                        
                    if websocket:
                        await websocket.send_json({"type": "llm_text", "content": content})
                elif event.delta.type == "input_json_delta":
                    tool_input_json += event.delta.partial_json
            elif event.type == "content_block_start":
                if event.content_block.type == "tool_use":
                    tool_use = event.content_block
                    # Reasoning trace (Requirement: visible reasoning trace)
                    if websocket:
                        await websocket.send_json({"type": "reasoning", "content": f"Thinking: Calling tool {tool_use.name}..."})

        # Signal end of text to TTS
        if buffer:
            await text_queue.put(buffer)
        await text_queue.put(None)
        await tts_task

        # 4. Process Tool Call if any
        if tool_use:
            tool_name = tool_use.name
            tool_id = tool_use.id
            import json
            args = json.loads(tool_input_json)
            
            logger.info(f"Running tool {tool_name} with args {args}")
            result = await llm_service.run_tool(tool_name, args, appointment_service)
            
            # Send tool result to UI
            if websocket:
                await websocket.send_json({"type": "reasoning", "content": f"Tool Result: {result}"})

            # Update history with tool use and result
            conversation_history.append({
                "role": "assistant",
                "content": [
                    {"type": "text", "text": full_response},
                    {"type": "tool_use", "id": tool_id, "name": tool_name, "input": args}
                ]
            })
            conversation_history.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": str(result)
                    }
                ]
            })
            
            # Recursive call for final response
            await llm_callback(None, True, websocket, session_id, language, confidence=confidence, start_time=t0)
        
        elif full_response:
            conversation_history.append({"role": "assistant", "content": full_response})
            memory_manager.save_session(session_id, conversation_history)
            logger.info(f"Agent: {full_response}")
        
        memory_manager.save_session(session_id, conversation_history)
    except asyncio.CancelledError:
        logger.info(f"Task for {session_id} was cancelled (interrupted).")
        raise
    except Exception as e:
        logger.error(f"CRITICAL ERROR in llm_callback: {e}", exc_info=True)
        if websocket:
            message = "The assistant is temporarily busy. Please repeat your last sentence."
            if "overloaded" not in str(e).lower():
                message = str(e)
            await websocket.send_json({"type": "error", "message": message})
    finally:
        state["llm_in_flight"] = False

@app.websocket("/ws/voice")
async def voice_websocket(websocket: WebSocket, session_id: str = "default", language: str = "en", sample_rate: int = 48000):
    await websocket.accept()
    logger.info(f"Client connected: session={session_id}, lang={language}, rate={sample_rate}")
    session_runtime.setdefault(
        session_id,
        {
            "assistant_speaking": False,
            "assistant_speaking_until": 0.0,
            "barge_in_armed": False,
            "last_user_text": "",
            "last_user_text_ts": 0.0,
            "llm_in_flight": False,
            "pending_llm_task": None,
            "pending_user_text": "",
            "pending_confidence": 0.0,
            "pending_start_time": 0.0,
        },
    )
    
    # 5. Turn Management & Speech Detection (Requirement: Turn Management & VAD)
    async def delayed_llm_launch(text, is_final, confidence, start_time):
        state = session_runtime.setdefault(session_id, {})
        clean = (text or "").strip()
        if not clean:
            return

        prior_text = (state.get("pending_user_text") or "").strip()
        if prior_text and clean.lower() not in prior_text.lower():
            state["pending_user_text"] = f"{prior_text} {clean}".strip()
        elif not prior_text:
            state["pending_user_text"] = clean

        state["pending_confidence"] = max(float(state.get("pending_confidence", 0.0)), float(confidence or 0.0))
        state["pending_start_time"] = start_time or state.get("pending_start_time")

        existing = state.get("pending_llm_task")
        if existing and not existing.done():
            existing.cancel()

        async def _run_after_delay():
            try:
                # Intentional pause before response generation for natural turn-taking.
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                return

            queued_text = (state.pop("pending_user_text", "") or "").strip()
            queued_conf = float(state.pop("pending_confidence", 0.0) or 0.0)
            queued_start = state.pop("pending_start_time", None)
            state["pending_llm_task"] = None
            if not queued_text:
                return
            if state.get("llm_in_flight"):
                logger.info("Deferring queued STT final while LLM turn is active")
                state["pending_user_text"] = queued_text
                state["pending_confidence"] = queued_conf
                state["pending_start_time"] = queued_start
                state["pending_llm_task"] = asyncio.create_task(_run_after_delay())
                return

            task = asyncio.create_task(
                llm_callback(
                    queued_text,
                    True,
                    websocket,
                    session_id,
                    language,
                    queued_conf,
                    queued_start,
                )
            )
            active_sessions[session_id] = task

        state["pending_llm_task"] = asyncio.create_task(_run_after_delay())

    async def wrapped_callback(text, is_final=False, confidence=0.0):
        # Record start time for latency measurement
        import time
        state = session_runtime.setdefault(session_id, {})
        now = time.time()
        clean = (text or "").strip()

        if clean and (state.get("assistant_speaking") or now < state.get("assistant_speaking_until", 0.0)):
            if state.get("barge_in_armed"):
                logger.info("Barge-in detected; interrupting assistant output")
                state["barge_in_armed"] = False
                await interrupt_session(session_id, websocket)

        if is_final:
            clean = (text or "").strip()
            prev = state.get("last_user_text", "")
            prev_ts = float(state.get("last_user_text_ts", 0.0))
            if clean and clean == prev and (now - prev_ts) < 2.0:
                logger.debug("Ignoring duplicate STT final: %s", clean)
                return
            state["last_user_text"] = clean
            state["last_user_text_ts"] = now

        start_time = time.time() if is_final else None

        # Immediate UI feedback for transcription
        await websocket.send_json({
            "type": "stt_text", 
            "content": text, 
            "is_final": is_final
        })
        # Proceed to LLM only when final
        if is_final:
            logger.info(f"STT Final -> Triggering LLM: {text} (conf={confidence:.2f})")
            # Run delayed LLM launch in background so STT loop stays non-blocking.
            asyncio.create_task(
                delayed_llm_launch(text, is_final, confidence, start_time)
            )

    stt_handler = SarvamHandler(callback=wrapped_callback)
    stt_started = await stt_handler.start(
        language=language + "-IN" if language != "en" else "en-US",
        sample_rate=sample_rate,
    )
    if not stt_started:
        await websocket.send_json({
            "type": "error",
            "message": "STT initialization failed. Check SARVAM_API_KEY and backend logs.",
        })
        await websocket.close(code=1011)
        return
    
    try:
        while True:
            data = await websocket.receive_bytes()
            await stt_handler.send_data(data)
    except WebSocketDisconnect:
        logger.info(f"Client disconnected: {session_id}")
    except Exception as e:
        logger.error(f"WebSocket error in {session_id}: {e}")
    finally:
        pending_task = session_runtime.get(session_id, {}).get("pending_llm_task")
        if pending_task and not pending_task.done():
            pending_task.cancel()
        session_runtime.pop(session_id, None)
        await stt_handler.stop()

@app.post("/campaign/trigger")
async def trigger_campaign(patient_id: str, topic: str):
    # In practice, this would initiate a call
    # For the demo, we log and return success
    logger.info(f"Manual campaign trigger: {patient_id} for {topic}")
    return {"status": "triggered", "patient_id": patient_id}

@app.get("/health")
async def health_check():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
